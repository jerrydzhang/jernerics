"""DashboardService investigation reads, scope materialization, writes."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from dash import dcc, html
from dash.development.base_component import Component
from dash_ag_grid import AgGrid
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionEndEvent,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    ManualParamEvent,
    Selection,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    decode_selection,
    encode_selection,
    materialize_selection,
)
from jernerics_server.dashboard import analysis, workspace
from jernerics_server.dashboard import sweep as sweep_page
from jernerics_server.dashboard.analysis import (
    EMPTY_TRAY,
    investigation_scope_state,
    points_tab,
    seed_sweeps_from_search,
)
from jernerics_server.dashboard.callbacks import page_content
from jernerics_server.dashboard.routes import ROUTES_BASE
from jernerics_server.dashboard.service import (
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
)
from jernerics_server.dashboard.workspace import (
    editor_factor_options,
    editor_outcome_options,
    editor_preview_panel,
)
from jernerics_server.ingest import IngestService
from jernerics_server.investigations import InvestigationService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store

LAB = "lab"

VAL_SWEEP = uuid.UUID("aa310000-0000-4000-8000-000000000001")
INC_SWEEP = uuid.UUID("aa310000-0000-4000-8000-000000000002")
BAD_SWEEP = uuid.UUID("aa310000-0000-4000-8000-000000000003")
LONE_SWEEP = uuid.UUID("aa310000-0000-4000-8000-000000000004")
FAR_SWEEP = uuid.UUID("aa310000-0000-4000-8000-000000000005")

VAL_TRIAL = uuid.UUID("cc310000-0000-4000-8000-000000000001")
INC_TRIAL = uuid.UUID("cc310000-0000-4000-8000-000000000002")
BAD_TRIAL = uuid.UUID("cc310000-0000-4000-8000-000000000003")
VAL_EXEC = uuid.UUID("dd310000-0000-4000-8000-000000000001")
INC_EXEC = uuid.UUID("dd310000-0000-4000-8000-000000000002")
BAD_EXEC = uuid.UUID("dd310000-0000-4000-8000-000000000003")

OUTCOME = "heldout_rmse"


def _seed_events() -> list:
    """Project lab: val (completed, outcome), inc (running sweep whose
    completed trial carries the outcome), bad (completed, later marked
    invalid), lone (no investigation), and far (another project)."""
    now = datetime.now(UTC)

    def at(seconds_ago: float) -> datetime:
        return now - timedelta(seconds=seconds_ago)

    def event(cls, seconds_ago: float, **kwargs):
        return cls(event_id=uuid.uuid4(), recorded_at=at(seconds_ago), **kwargs)

    def sweep(sweep_id, name, seconds_ago, project=LAB, state="completed"):
        return event(
            SweepSnapshotEvent,
            seconds_ago,
            project=project,
            sweep_id=sweep_id,
            name=name,
            state=state,
        )

    def scored_trial(trial_id, sweep_id, execution_id, seconds_ago):
        return [
            event(
                TrialSnapshotEvent,
                seconds_ago,
                trial_id=trial_id,
                sweep_id=sweep_id,
                number=0,
                state=TrialState.COMPLETED,
                retry_root_trial_id=trial_id,
            ),
            event(
                ExecutionStartEvent,
                seconds_ago - 10,
                execution_id=execution_id,
                trial_id=trial_id,
                hostname="node00",
                started_at=at(seconds_ago - 10),
            ),
            event(
                ExecutionEndEvent,
                seconds_ago - 20,
                execution_id=execution_id,
                ended_at=at(seconds_ago - 20),
                outcome="success",
                exit_code=0,
            ),
            event(
                ValueEvent,
                seconds_ago - 30,
                trial_id=trial_id,
                key=OUTCOME,
                step=0,
                value=0.5,
            ),
        ]

    return [
        sweep(VAL_SWEEP, "val-sweep", 1000),
        sweep(INC_SWEEP, "inc-sweep", 900, state="running"),
        sweep(BAD_SWEEP, "bad-sweep", 800),
        sweep(LONE_SWEEP, "lone-sweep", 700),
        sweep(FAR_SWEEP, "far-sweep", 600, project="other"),
        *scored_trial(VAL_TRIAL, VAL_SWEEP, VAL_EXEC, 990),
        *scored_trial(INC_TRIAL, INC_SWEEP, INC_EXEC, 890),
        *scored_trial(BAD_TRIAL, BAD_SWEEP, BAD_EXEC, 790),
    ]


@pytest.fixture
def store(tmp_path) -> Store:
    store = Store(tmp_path / "investigations.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_seed_events())
    )
    assert not result.conflicts
    store.mark_sweep_invalid(str(BAD_SWEEP), "upstream data error")
    return store


@pytest.fixture
def service(store) -> DashboardService:
    return DashboardService(QueryService(store), store)


@pytest.fixture
def shared(store) -> InvestigationService:
    return InvestigationService(store)


@pytest.fixture
def compare(shared) -> str:
    record = shared.create(
        LAB,
        "alpha-compare",
        "lr",
        OUTCOME,
        members=[str(VAL_SWEEP), str(INC_SWEEP), str(BAD_SWEEP)],
    )
    shared.create(LAB, "beta-solo", "seed", OUTCOME, members=[str(VAL_SWEEP)])
    archived = shared.create(LAB, "gamma-empty", "lr", OUTCOME)
    shared.archive(str(archived.id))
    return str(record.id)


def _sweep_updated_ns(store: Store, sweep_id: uuid.UUID) -> int:
    _, rows = store.query(
        "SELECT updated_ns FROM sweeps WHERE sweep_id = ?", [str(sweep_id)]
    )
    return rows[0][0]


class TestInvestigationsIndex:
    def test_coverage_facts_over_mixed_members(self, service, compare):
        rows = {row.name: row for row in service.investigations_index(LAB)}
        assert set(rows) == {"alpha-compare", "beta-solo"}
        alpha = rows["alpha-compare"]
        assert alpha.investigation_id == compare
        assert (alpha.factor, alpha.outcome) == ("lr", OUTCOME)
        assert alpha.member_count == 3
        assert alpha.with_outcome == 3
        assert alpha.completed == 2
        assert alpha.invalid == 1

    def test_last_activity_comes_from_members(self, service, store, compare):
        alpha = next(
            row
            for row in service.investigations_index(LAB)
            if row.name == "alpha-compare"
        )
        assert alpha.last_activity_ns == max(
            _sweep_updated_ns(store, sweep_id)
            for sweep_id in (VAL_SWEEP, INC_SWEEP, BAD_SWEEP)
        )

    def test_archived_investigations_hidden_by_default(self, service, compare):
        assert service.investigations_index(LAB, include_archived=True) != (
            service.investigations_index(LAB)
        )
        hidden = {row.name for row in service.investigations_index(LAB)} ^ {
            row.name for row in service.investigations_index(LAB, include_archived=True)
        }
        assert hidden == {"gamma-empty"}

    def test_empty_investigation_row_has_zero_coverage(self, service, shared, compare):
        archived = shared.detail_by_name(LAB, "gamma-empty")
        assert archived.coverage.members == 0
        assert archived.coverage.with_outcome == 0
        assert archived.coverage.last_activity_ns is None

    def test_archived_sweep_members_still_counted(self, service, store, compare):
        store.archive_sweep(str(VAL_SWEEP))
        alpha = next(
            row
            for row in service.investigations_index(LAB)
            if row.name == "alpha-compare"
        )
        assert (alpha.member_count, alpha.with_outcome, alpha.completed) == (3, 3, 2)


class TestUnorganized:
    def test_project_sweeps_in_no_investigation(self, service, compare):
        assert [row.name for row in service.unorganized(LAB)] == ["lone-sweep"]
        assert [row.name for row in service.unorganized("other")] == ["far-sweep"]

    def test_rows_shaped_like_sweep_summaries(self, service, compare):
        lone = service.unorganized(LAB)[0]
        assert str(lone.sweep_id) == str(LONE_SWEEP)
        assert lone.state == "no-data"
        assert lone.incomplete is False

    def test_archived_investigation_still_organizes(self, service, store, shared):
        record = shared.create(
            LAB, "alpha-compare", "lr", OUTCOME, members=[str(VAL_SWEEP)]
        )
        shared.archive(str(record.id))
        assert {row.name for row in service.unorganized(LAB)} == {
            "inc-sweep",
            "bad-sweep",
            "lone-sweep",
        }


class TestInvestigationDetail:
    def test_passthrough_matches_shared_service(self, service, shared, compare):
        assert service.investigation_detail(compare) == shared.detail(compare)

    def test_unknown_id_rejected_with_message(self, service):
        with pytest.raises(CurationRejectedError, match="no investigation"):
            service.investigation_detail(str(uuid.uuid4()))


class TestInvestigationScope:
    def test_materializes_to_plain_selection(self, service, shared, compare):
        record = shared.detail(compare).investigation
        assert materialize_selection(record) == Selection(
            project=LAB, sweeps=(VAL_SWEEP, INC_SWEEP, BAD_SWEEP)
        )

    def test_scope_group_holds_members_as_tray_sweeps(self, service, compare):
        record = service.investigation_detail(compare).investigation
        tray, _scoped = analysis.investigation_scope_state(record.members, None)
        assert tray["sweeps"] == sorted(
            str(sweep_id) for sweep_id in (VAL_SWEEP, INC_SWEEP, BAD_SWEEP)
        )
        assert tray["trials"] == []
        assert tray["families"] == []
        assert tray["executions"] == []
        assert tray["expand"] is False

    def test_url_token_round_trips_to_the_same_scope(self, service, shared, compare):
        record = shared.detail(compare).investigation
        token = encode_selection(materialize_selection(record))
        selection = decode_selection(token)
        tray, _scoped = analysis.investigation_scope_state(
            service.investigation_detail(compare).investigation.members, None
        )
        assert {key: tray[key] for key in EMPTY_TRAY} == {
            "sweeps": sorted(str(s) for s in (selection.sweeps or ())),
            "trials": [],
            "families": [],
            "executions": [],
            "expand": False,
        }

    def test_known_member_narrows_to_one_sweep(self, service, compare):
        record = service.investigation_detail(compare).investigation
        tray, scoped = analysis.investigation_scope_state(
            record.members, str(INC_SWEEP)
        )
        assert scoped == str(INC_SWEEP)
        assert tray["sweeps"] == [str(INC_SWEEP)]

    def test_unknown_member_folds_back_to_the_full_cohort(self, service, compare):
        record = service.investigation_detail(compare).investigation
        tray, scoped = analysis.investigation_scope_state(record.members, "ghost")
        assert scoped is None
        assert tray["sweeps"] == sorted(
            str(sweep_id) for sweep_id in (VAL_SWEEP, INC_SWEEP, BAD_SWEEP)
        )


class TestInvestigationWrites:
    def test_create_visible_through_shared_service(self, service, shared):
        record = service.create_investigation(
            LAB, "alpha-compare", "lr", OUTCOME, members=[str(VAL_SWEEP)]
        )
        assert shared.detail(str(record.id)).investigation == record

    def test_create_conflicting_body_rejected(self, service, compare):
        with pytest.raises(CurationRejectedError, match="already exists"):
            service.create_investigation(LAB, "alpha-compare", "seed", OUTCOME)

    def test_membership_edits_flow_through_shared_service(
        self, service, shared, compare
    ):
        service.remove_investigation_members(compare, [str(BAD_SWEEP)])
        assert shared.detail(compare).investigation.members == (
            VAL_SWEEP,
            INC_SWEEP,
        )
        service.add_investigation_members(compare, [str(LONE_SWEEP)])
        assert LONE_SWEEP in shared.detail(compare).investigation.members
        service.set_investigation_members(compare, [str(VAL_SWEEP)])
        assert shared.detail(compare).investigation.members == (VAL_SWEEP,)

    def test_cross_project_member_rejected(self, service, compare):
        with pytest.raises(CurationRejectedError, match="belongs to project"):
            service.add_investigation_members(compare, [str(FAR_SWEEP)])

    def test_unknown_member_sweep_rejected(self, service, compare):
        with pytest.raises(CurationRejectedError, match="no sweep matches this id"):
            service.add_investigation_members(compare, [str(uuid.uuid4())])

    def test_archive_and_restore_round_trip(self, service, shared, compare):
        archived = service.archive_investigation(compare)
        assert archived.archived_ns is not None
        assert shared.detail(compare).investigation.archived_ns is not None
        restored = service.restore_investigation(compare)
        assert restored.archived_ns is None


class TestInvestigationsUnavailable:
    def test_reads_and_writes_need_a_store(self, store):
        service = DashboardService(QueryService(store))
        with pytest.raises(CurationUnavailableError):
            service.investigations_index(LAB)
        with pytest.raises(CurationUnavailableError):
            service.create_investigation(LAB, "alpha-compare", "lr", OUTCOME)


class TestInvestigationsIndexPage:
    """The Investigations index on the new shell renders the
    DashboardService facts as a table, the Unorganized list, and the
    editor action (jernerics-g5rw.7, re-skinned)."""

    def _page(self, service, now_ns=1_000_000_000_000):
        return workspace.investigations_index_page(service, LAB, now_ns)

    def _index_rows(self, page):
        section = next(
            node
            for node in _walk_children(page)
            if isinstance(node, html.Section)
            and "investigations-index" in (node.className or "")
        )
        table = _of(section, html.Table)[0]
        return _of(table, html.Tr)[1:]  # head row first

    def test_reads_need_a_store(self, store):
        read_only = DashboardService(QueryService(store))
        page = self._page(read_only)
        assert "no write store" in str(page)

    def test_index_rows_match_service_facts(self, service, compare):
        page = self._page(service)
        rows = self._index_rows(page)
        service_rows = {
            row.investigation_id: row for row in service.investigations_index(LAB)
        }
        assert len(rows) == len(service_rows)
        for row in rows:
            cells = _of(row, html.Td)
            link = _of(cells[0], html.A)[0]
            facts = service_rows[link.href.rsplit("/", 1)[1]]
            assert _text(link) == facts.name
            assert _text(cells[1]) == facts.factor
            assert _text(cells[2]) == facts.outcome
            assert _text(cells[3]) == str(facts.member_count)
            assert _text(cells[4]) == (
                f"{facts.with_outcome} with outcome · "
                f"{facts.member_count - facts.completed} incomplete · "
                f"{facts.invalid} invalid"
            )
            edit = _of(cells[6], html.A)[0]
            assert edit.href == (
                f"{ROUTES_BASE}/project/{LAB}/investigation/"
                f"{facts.investigation_id}/edit"
            )

    def test_archived_investigations_stay_off_the_default_index(self, service, compare):
        page = self._page(service)
        assert len(self._index_rows(page)) == 2  # gamma-empty is archived

    def test_unorganized_lists_sweeps_in_no_investigation(self, service, compare):
        page = self._page(service)
        details = _of(page, html.Details)[0]
        links = {_text(link): link.href for link in _walk_anchors(details)}
        assert str(LONE_SWEEP) not in links
        lone = next(
            href for name, href in links.items() if name and name != "Show list"
        )
        assert lone == f"{ROUTES_BASE}/project/{LAB}/sweep/{LONE_SWEEP}"
        rendered = str(page)
        assert "Unorganized" in rendered
        assert "1 sweep not in any Investigation" in rendered

    def test_new_investigation_action_targets_the_editor_route(self, service, compare):
        page = self._page(service)
        link = next(
            node for node in _walk_anchors(page) if node.children == "New Investigation"
        )
        assert link.href == f"{ROUTES_BASE}/project/{LAB}/investigation/new"

    def test_route_parses_to_the_index_kind(self):
        from jernerics_server.dashboard.routes import parse_route

        spec = parse_route(f"{ROUTES_BASE}/project/{LAB}/investigations")
        assert (spec.kind, spec.object_id, spec.sub_id) == (
            "investigations",
            LAB,
            None,
        )

    def test_page_content_renders_the_index_page(self, service, compare):
        page, polls = page_content(
            f"{ROUTES_BASE}/project/{LAB}/investigations", service
        )
        assert polls is False
        assert "Investigations" in _text(page)
        assert "not in any Investigation" in _text(page)


def _of(node, kind):
    return [item for item in _walk_children(node) if isinstance(item, kind)]


def _walk_anchors(component):
    from dash import html

    return [node for node in _walk_children(component) if isinstance(node, html.A)]


def _walk_children(node):
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from _walk_children(child)
    elif isinstance(children, Component):
        yield from _walk_children(children)


# -- jernerics-g5rw.8: Compare view, workspace shell, member editor -----

CMP = "cmp"

CS1 = uuid.UUID("aa410000-0000-4000-8000-000000000001")
CS2 = uuid.UUID("aa410000-0000-4000-8000-000000000002")
CS3 = uuid.UUID("aa410000-0000-4000-8000-000000000003")
CS4 = uuid.UUID("aa410000-0000-4000-8000-000000000004")
CS5 = uuid.UUID("aa410000-0000-4000-8000-000000000005")

CT1 = uuid.UUID("cc410000-0000-4000-8000-000000000001")
CT2 = uuid.UUID("cc410000-0000-4000-8000-000000000002")
CT3 = uuid.UUID("cc410000-0000-4000-8000-000000000003")
CT4 = uuid.UUID("cc410000-0000-4000-8000-000000000004")
CT5 = uuid.UUID("cc410000-0000-4000-8000-000000000005")

CE1 = uuid.UUID("dd410000-0000-4000-8000-000000000001")
CE2 = uuid.UUID("dd410000-0000-4000-8000-000000000002")
CE3 = uuid.UUID("dd410000-0000-4000-8000-000000000003")
CE4 = uuid.UUID("dd410000-0000-4000-8000-000000000004")
CE5 = uuid.UUID("dd410000-0000-4000-8000-000000000005")

_SIG = ("kde_bandwidth", 0.1), ("n_kde", 20)
_SIG_LABEL = "kde_bandwidth=0.1 · n_kde=20"


def _cmp_events() -> list:
    """Project cmp: f01 (two completed trials on one signature), f02
    (one), f03 (invalid but data-bearing), f04 (a disjoint signature),
    and a trial-less sweep."""
    now = datetime.now(UTC)

    def at(seconds_ago: float) -> datetime:
        return now - timedelta(seconds=seconds_ago)

    def event(cls, seconds_ago: float, **kwargs):
        return cls(event_id=uuid.uuid4(), recorded_at=at(seconds_ago), **kwargs)

    def sweep(sweep_id, name, seconds_ago, project=CMP):
        return event(
            SweepSnapshotEvent,
            seconds_ago,
            project=project,
            sweep_id=sweep_id,
            name=name,
            state="completed",
        )

    def scored(number, trial_id, sweep_id, execution_id, params, value, factor):
        return [
            event(
                ManualParamEvent,
                510 - number,
                trial_id=trial_id,
                key="problem",
                value=factor,
            ),
            event(
                TrialSnapshotEvent,
                500 - number,
                trial_id=trial_id,
                sweep_id=sweep_id,
                number=number,
                state=TrialState.COMPLETED,
                retry_root_trial_id=trial_id,
                params=FlatContext(dict(params)),
            ),
            event(
                ExecutionStartEvent,
                490 - number,
                execution_id=execution_id,
                trial_id=trial_id,
                hostname="node00",
                started_at=at(490 - number),
            ),
            event(
                ExecutionEndEvent,
                480 - number,
                execution_id=execution_id,
                ended_at=at(480 - number),
                outcome="success",
                exit_code=0,
            ),
            event(
                ValueEvent,
                470 - number,
                trial_id=trial_id,
                key=OUTCOME,
                step=0,
                value=value,
            ),
        ]

    return [
        sweep(CS1, "cmp_f01", 900),
        sweep(CS2, "cmp_f02", 800),
        sweep(CS3, "cmp_f03", 700),
        sweep(CS4, "cmp_f04", 600),
        sweep(CS5, "cmp_lone", 500),
        *scored(0, CT1, CS1, CE1, _SIG, 0.5, "f01"),
        *scored(1, CT2, CS1, CE2, _SIG, 0.7, "f01"),
        *scored(0, CT3, CS2, CE3, _SIG, 1.0, "f02"),
        *scored(0, CT4, CS3, CE4, _SIG, 9.9, "f03"),
        *scored(0, CT5, CS4, CE5, (("kde_bandwidth", 0.3), ("n_kde", 20)), 3.0, "f04"),
    ]


@pytest.fixture
def cmp_store(tmp_path) -> Store:
    store = Store(tmp_path / "compare.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_cmp_events())
    )
    assert not result.conflicts
    store.mark_sweep_invalid(str(CS3), "upstream data error")
    return store


@pytest.fixture
def cmp_service(cmp_store) -> DashboardService:
    return DashboardService(QueryService(cmp_store), cmp_store)


@pytest.fixture
def cmp_shared(cmp_store) -> InvestigationService:
    return InvestigationService(cmp_store)


@pytest.fixture
def sig(cmp_shared) -> SimpleNamespace:
    """Four investigations over the cmp sweeps: the canonical compare
    set (with the invalid member), a disjoint-signature pair, a hollow
    member set, and an invalid-only one."""
    compare = cmp_shared.create(
        CMP,
        "sig-compare",
        "problem",
        OUTCOME,
        members=[str(CS1), str(CS2), str(CS3)],
    )
    disjoint = cmp_shared.create(
        CMP, "disjoint", "problem", OUTCOME, members=[str(CS1), str(CS4)]
    )
    hollow = cmp_shared.create(CMP, "hollow", "problem", OUTCOME, members=[str(CS5)])
    only_invalid = cmp_shared.create(
        CMP, "only-invalid", "problem", OUTCOME, members=[str(CS3)]
    )
    return SimpleNamespace(
        compare=str(compare.id),
        disjoint=str(disjoint.id),
        hollow=str(hollow.id),
        only_invalid=str(only_invalid.id),
    )


class TestInvestigationCompare:
    """Compare row derivation against the seeded facts."""

    def test_exact_signature_match_pools_trials_per_member(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.compare)
        assert doc.signature_keys == ("kde_bandwidth", "n_kde")
        assert doc.analyzable == (str(CS1), str(CS2))
        assert len(doc.signatures) == 1
        row = doc.signatures[0]
        assert row.label == _SIG_LABEL
        assert row.values == {str(CS1): 0.6, str(CS2): 1.0}
        assert row.common and row.matched == 2

    def test_invalid_members_excluded_from_analysis_by_default(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.compare)
        assert doc.excluded_data_bearing == 1
        assert str(CS3) not in doc.analyzable
        assert all(
            str(CS3) not in row.values or row.values.get(str(CS3)) is None
            for row in doc.signatures
        )

    def test_include_invalid_toggle_expands_the_analysis_set(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.compare, include_invalid=True)
        assert doc.analyzable == (str(CS1), str(CS2), str(CS3))
        row = doc.signatures[0]
        assert row.values[str(CS3)] == pytest.approx(9.9)
        assert row.common and row.matched == 3

    def test_member_rows_carry_derived_factor_state_and_usable(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.compare)
        by_id = {member.sweep_id: member for member in doc.members}
        assert by_id[str(CS1)].factor_value == "f01"  # a name token
        assert by_id[str(CS1)].usable == 2
        assert by_id[str(CS2)].usable == 1
        assert by_id[str(CS3)].invalid and by_id[str(CS3)].usable == 1
        assert by_id[str(CS3)].factor_value == "f03"

    def test_no_global_overlap_renders_no_manufactured_ranking(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.disjoint)
        assert doc.signatures and all(row.matched == 1 for row in doc.signatures)
        body = workspace.compare_body(doc, CMP, OUTCOME, sig.disjoint, False)
        assert "no global overlap" in _text(body)
        assert not [
            node
            for section in body
            for node in _walk_children(section)
            if isinstance(node, dcc.Graph)
        ]

    def test_empty_analysis_set_names_the_exclusions(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.only_invalid)
        assert doc.analyzable == ()
        text = str(workspace.compare_empty_state(doc, include_invalid=False))
        assert "No analyzable members in the analysis set" in text
        assert "1 data-bearing members are marked invalid" in text
        assert "include invalid members in analysis" in text

    def test_compare_reads_match_the_shared_service(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.compare)
        assert {member.sweep_id for member in doc.members} == {
            str(sweep)
            for sweep in cmp_service.investigation_detail(
                sig.compare
            ).investigation.members
        }


class TestInvestigationWorkspacePage:
    """The new-shell Investigation page: crumbs, header, the view row,
    and the Compare view assembly."""

    def test_shell_names_project_investigation_and_default_view(self, cmp_service, sig):
        page = workspace.investigation_page(cmp_service, CMP, sig.compare)
        text = _text(page)
        assert "sig-compare" in text
        assert (
            "factor problem · outcome heldout_rmse (final) · matching by "
            "exact sampled signature" in text
        )
        crumb = next(
            node
            for node in _walk_children(page)
            if isinstance(node, html.Div) and node.className == "crumb"
        )
        crumb_links = [
            node.href
            for node in _walk_children(crumb)
            if isinstance(node, html.A) and node.href
        ]
        assert crumb_links == [
            f"{ROUTES_BASE}/project/{CMP}",
            f"{ROUTES_BASE}/project/{CMP}/investigations",
        ]

    def test_view_row_mounts_links_with_query_state(self, cmp_service, sig):
        page = workspace.investigation_page(cmp_service, CMP, sig.compare)
        seg = _string_id_node(page, "inv-tabs")
        links = _of(seg, html.A)
        assert [link.children for link in links] == [
            "Compare",
            "Series",
            "Points",
            "Search",
        ]
        assert [getattr(link, "className", None) for link in links] == [
            "on",
            None,
            None,
            None,
        ]
        base = f"{ROUTES_BASE}/project/{CMP}/investigation/{sig.compare}"
        assert links[0].href == base
        assert links[1].href == f"{base}?view=series"
        assert links[2].href == f"{base}?view=points"
        assert links[3].href == f"{base}?view=search"
        actions = {
            node.children: node.href
            for node in _walk_children(page)
            if isinstance(node, html.A) and getattr(node, "className", None) == "btn"
        }
        assert actions["Open in Python"] == f"{base}?view=python"
        assert actions["Edit members"] == f"{base}/edit"

    def test_python_view_exports_the_member_selection_token(self, cmp_service, sig):
        page = _inv_page(cmp_service, sig.compare, view="python")
        clipboards = [
            node for node in _walk_children(page) if isinstance(node, dcc.Clipboard)
        ]
        selection = decode_selection(clipboards[0].content)
        assert selection.project == CMP
        assert set(selection.sweeps or ()) == {CS1, CS2, CS3}

    def test_coverage_strip_matches_the_derived_members(self, cmp_service, sig):
        doc = cmp_service.investigation_compare(sig.compare)
        strip = workspace.coverage_strip(doc)
        numbers = [
            node.children for node in _walk_children(strip) if isinstance(node, html.B)
        ]
        assert numbers == [3, 2, 1, 3, 0]  # members/valid/invalid/outcome/incomplete

    def test_include_toggle_mounts_only_with_invalid_members(
        self, cmp_service, cmp_shared, sig
    ):
        page = workspace.investigation_page(cmp_service, CMP, sig.compare)
        assert _string_id_node(page, "inv-include-invalid") is not None
        pair = cmp_shared.create(
            CMP, "pair", "problem", OUTCOME, members=[str(CS1), str(CS2)]
        )
        clean = workspace.investigation_page(cmp_service, CMP, str(pair.id))
        assert _string_id_node(clean, "inv-include-invalid") is None

    def test_only_invalid_page_renders_the_exclusion_empty_state(
        self, cmp_service, sig
    ):
        page = workspace.investigation_page(cmp_service, CMP, sig.only_invalid)
        text = str(page)
        assert "No analyzable members in the analysis set" in text
        assert "1 data-bearing members are marked invalid" in text

    def test_page_content_maps_routes_storeless_and_unknown_ids(
        self, cmp_service, cmp_store, sig
    ):
        base = f"{ROUTES_BASE}/project/{CMP}/investigation"
        page, polls = page_content(f"{base}/{sig.compare}", cmp_service)
        assert "sig-compare" in str(page) and polls is False
        page, _ = page_content(f"{base}/{uuid.uuid4()}", cmp_service)
        assert "No investigation matches" in str(page)
        read_only = DashboardService(QueryService(cmp_store))
        page, _ = page_content(f"{base}/{sig.compare}", read_only)
        assert "no write store" in str(page)
        editor, polls = page_content(
            f"{base}/new",
            cmp_service,
            search=f"?sweeps={CS1},{CS2}",
        )
        assert "New Investigation" in str(editor) and polls is False


class TestInvestigationEditorPages:
    """Create and edit are distinct flows; the preview carries real
    coverage counts before anything is written."""

    def test_create_flow_seeds_members_preview_and_gating(self, cmp_service, sig):
        seed = seed_sweeps_from_search(f"?sweeps={CS2},{CS1},deadbeef")
        assert seed == sorted({str(CS1), str(CS2), "deadbeef"})
        page = workspace.investigation_edit_page(cmp_service, CMP, None, seed)
        state = _first_pattern(page, "inv-edit-state").data
        assert state["picked"] == seed and state["saved"] == []
        picks = {
            node.id["inv-edit-pick"]: node.value
            for node in _pattern_nodes(page, "inv-edit-pick")
        }
        assert set(picks) == {str(CS1), str(CS2), str(CS3), str(CS4), str(CS5)}
        assert picks[str(CS1)] == [str(CS1)] and picks[str(CS2)] == [str(CS2)]
        assert picks[str(CS3)] == []
        assert _first_pattern(page, "inv-edit-name") is not None
        assert _first_pattern(page, "inv-edit-save").disabled is True
        assert not _pattern_nodes(page, "inv-edit-discard")
        text = _text(_first_pattern(page, "inv-edit-preview"))
        assert "unknown sweep: no sweep with id deadbeef" in text

    def test_create_factor_options_carry_real_coverage(self, cmp_service, sig):
        page = workspace.investigation_edit_page(
            cmp_service, CMP, None, [str(CS1), str(CS2)]
        )
        factor = _first_pattern(page, "inv-edit-factor")
        assert {
            "label": "param problem — 2 of 2 members",
            "value": "problem",
        } in factor.options
        outcome = _first_pattern(page, "inv-edit-outcome")
        assert {
            "label": "heldout_rmse — 2 of 2 members",
            "value": OUTCOME,
        } in outcome.options

    def test_edit_flow_preselects_saved_members_without_create_controls(
        self, cmp_service, sig
    ):
        page = workspace.investigation_edit_page(cmp_service, CMP, sig.compare, [])
        state = _first_pattern(page, "inv-edit-state").data
        assert state["picked"] == state["saved"] == [str(CS1), str(CS2), str(CS3)]
        picks = {
            node.id["inv-edit-pick"]: node.value
            for node in _pattern_nodes(page, "inv-edit-pick")
        }
        assert picks == {
            str(CS1): [str(CS1)],
            str(CS2): [str(CS2)],
            str(CS3): [str(CS3)],
            str(CS4): [],
            str(CS5): [],
        }
        assert not _pattern_nodes(page, "inv-edit-name")
        assert not _pattern_nodes(page, "inv-edit-factor")
        assert _first_pattern(page, "inv-edit-save").disabled is False
        assert _first_pattern(page, "inv-edit-discard") is not None

    def test_preview_panel_reports_pending_diff_and_coverage(self, cmp_service, sig):
        picked = [str(CS1), str(CS2), "deadbeef"]
        preview = cmp_service.investigation_preview(CMP, picked)
        panel = editor_preview_panel(
            preview,
            {"picked": picked, "saved": [str(CS1)], "name": "", "factor": None},
        )
        text = _text(panel)
        assert "2 project members picked" in text
        assert "+2 -0 (unsaved)" in text
        assert "param problem — 2 of 2 members" in text
        assert "heldout_rmse — 2 of 2 members" in text
        assert "unknown sweep: no sweep with id deadbeef" in text
        assert "problem" in {
            option["value"] for option in editor_factor_options(preview)
        }
        assert editor_outcome_options(preview)[0]["value"] == OUTCOME

    def test_create_never_overwrites_an_existing_investigation(self, cmp_service, sig):
        record = cmp_service.create_investigation(
            CMP, "sig-compare", "problem", OUTCOME, members=[str(CS1)]
        )
        assert record.id == uuid.UUID(sig.compare)
        assert [str(sweep) for sweep in record.members] == [
            str(CS1),
            str(CS2),
            str(CS3),
        ]
        with pytest.raises(CurationRejectedError):
            cmp_service.create_investigation(
                CMP, "sig-compare", "f02", OUTCOME, members=[str(CS1)]
            )


class TestEditorCallbacks:
    """The mounted editor callbacks, driven through Dash's dispatch
    endpoint exactly as the browser would."""

    @pytest.fixture(scope="class")
    def mounted(self, tmp_path_factory):
        from jernerics_server.dashboard.app import build_dash_app
        from jernerics_server.dashboard.auth import DashboardContext
        from jernerics_server.dashboard.sessions import SessionSigner
        from jernerics_server.http import create_app

        root = tmp_path_factory.mktemp("editor-callbacks")
        store = Store(root / "callbacks.sqlite")
        result = IngestService(store).apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=_cmp_events())
        )
        assert not result.conflicts
        store.mark_sweep_invalid(str(CS3), "upstream data error")
        shared = InvestigationService(store)
        compare = shared.create(
            CMP,
            "sig-compare",
            "problem",
            OUTCOME,
            members=[str(CS1), str(CS2), str(CS3)],
        )
        service = DashboardService(QueryService(store), store)
        ctx = DashboardContext(
            api_key="secret123",
            queries=service.queries,
            service=service,
            signer=SessionSigner(b"\x00" * 32),
        )
        client = TestClient(
            create_app(store, api_key="secret123", dashboard=True),
            base_url="https://testserver",
        )
        response = client.post(
            "/dashboard/login", data={"api_key": "secret123"}, follow_redirects=False
        )
        assert response.status_code == 303
        return SimpleNamespace(
            client=client,
            callback_map=build_dash_app(ctx).callback_map,
            shared=shared,
            compare=str(compare.id),
        )

    @staticmethod
    def _key(callback_map, output_needle: str, input_needle: str) -> str:
        """Callback keys carry the output specs only; the input specs
        disambiguate callbacks that share an output."""
        for key, spec in callback_map.items():
            if output_needle not in key:
                continue
            if input_needle in json.dumps(spec.get("inputs", [])):
                return key
        raise AssertionError(f"no callback for {output_needle}/{input_needle}")

    @staticmethod
    def _single(value):
        """Pattern multi-output responses wrap values per matched component."""
        return value[0] if isinstance(value, list) and value else value

    @staticmethod
    def _out(result, name: str, prop: str):
        """Pattern-callback responses key outputs by the compact id."""
        return result["response"][f'{{"{name}":["ALL"]}}'][prop]

    def _dispatch(self, mounted, key, inputs, state=()):
        if isinstance(state, dict):
            state = [state]
        specs = [
            spec.split(".")
            for spec in key.removeprefix("..").removesuffix("..").split("...")
            if spec
        ]
        outputs = []
        for spec_id, prop in specs:
            try:
                resolved = json.loads(spec_id)
            except json.JSONDecodeError:
                resolved = spec_id
            outputs.append({"id": resolved, "property": prop.split("@")[0]})
        response = mounted.client.post(
            "/dashboard/_dash-update-component",
            json={
                "output": key,
                "outputs": outputs,
                "inputs": inputs,
                "state": list(state),
                "changedPropIds": [
                    f"{json.dumps(item['id'], separators=(',', ':'))}"
                    f".{item['property']}"
                    for group in inputs
                    for item in group
                ],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()

    _CREATE_ROUTE = f"{ROUTES_BASE}/project/{CMP}/investigation/new"
    _EDIT_ROUTE = f"{ROUTES_BASE}/project/{CMP}/investigation/<id>/edit"

    @staticmethod
    def _state(picked, saved, **extra):
        return {
            "picked": picked,
            "saved": saved,
            "name": extra.get("name"),
            "factor": extra.get("factor"),
            "outcome": extra.get("outcome"),
        }

    def test_checkbox_edits_update_the_picked_members(self, mounted):
        key = self._key(mounted.callback_map, "inv-edit-state", "inv-edit-pick")
        result = self._dispatch(
            mounted,
            key,
            inputs=[
                [
                    {
                        "id": [
                            {"inv-edit-pick": str(CS1)},
                            {"inv-edit-pick": str(CS2)},
                        ],
                        "property": "value",
                        "value": [[str(CS1)], []],
                    }
                ]
            ],
            state=[
                [
                    {
                        "id": {"inv-edit-pick": str(CS1)},
                        "property": "id",
                        "value": {"inv-edit-pick": str(CS1)},
                    },
                    {
                        "id": {"inv-edit-pick": str(CS2)},
                        "property": "id",
                        "value": {"inv-edit-pick": str(CS2)},
                    },
                ],
                [
                    {
                        "id": {"inv-edit-state": "members"},
                        "property": "data",
                        "value": self._state(
                            [str(CS1), str(CS2)], [str(CS1), str(CS2)]
                        ),
                    }
                ],
            ],
        )
        assert self._out(result, "inv-edit-state", "data")["picked"] == [str(CS1)]

    def test_state_edit_flows_to_preview_and_save_gating(self, mounted):
        key = self._key(mounted.callback_map, "inv-edit-preview", "inv-edit-state")
        result = self._dispatch(
            mounted,
            key,
            inputs=[
                [
                    {
                        "id": {"inv-edit-state": "members"},
                        "property": "data",
                        "value": self._state(
                            [str(CS1), str(CS2)],
                            [],
                            name="draft",
                            factor="problem",
                            outcome=OUTCOME,
                        ),
                    }
                ]
            ],
            state=[
                [
                    {
                        "id": {"inv-edit-pick": str(CS1)},
                        "property": "id",
                        "value": {"inv-edit-pick": str(CS1)},
                    },
                    {
                        "id": {"inv-edit-pick": str(CS2)},
                        "property": "id",
                        "value": {"inv-edit-pick": str(CS2)},
                    },
                ],
                [
                    {
                        "id": [
                            {"inv-edit-pick": str(CS1)},
                            {"inv-edit-pick": str(CS2)},
                        ],
                        "property": "value",
                        "value": [[], []],
                    }
                ],
                [
                    {
                        "id": {"inv-edit-mode": "all"},
                        "property": "id",
                        "value": {"inv-edit-mode": "all"},
                    },
                    {
                        "id": {"inv-edit-mode": "members"},
                        "property": "id",
                        "value": {"inv-edit-mode": "members"},
                    },
                ],
                {"id": "url", "property": "pathname", "value": self._CREATE_ROUTE},
            ],
        )
        assert self._out(result, "inv-edit-save", "disabled") == [False]
        text = _text(self._out(result, "inv-edit-preview", "children"))
        assert "2 project members picked" in text
        assert "+2 -0 (unsaved)" in text
        # The mounted checkboxes take the working selection (one entry
        # per row), and the seg's Members label follows the picks.
        assert self._out(result, "inv-edit-pick", "value") == [
            [str(CS1)],
            [str(CS2)],
        ]
        assert self._out(result, "inv-edit-mode", "children")[1] == "Members (2)"

    def test_save_on_edit_flow_replaces_and_syncs_members(self, mounted):
        key = self._key(mounted.callback_map, "url.pathname", "inv-edit-save")
        route = self._EDIT_ROUTE.replace("<id>", mounted.compare)
        result = self._dispatch(
            mounted,
            key,
            inputs=[
                [
                    {
                        "id": {"inv-edit-save": "save"},
                        "property": "n_clicks",
                        "value": 1,
                    }
                ]
            ],
            state=[
                [
                    {
                        "id": {"inv-edit-state": "members"},
                        "property": "data",
                        "value": self._state(
                            [str(CS1)], [str(CS1), str(CS2), str(CS3)]
                        ),
                    }
                ],
                {"id": "url", "property": "pathname", "value": route},
            ],
        )
        state = self._single(self._out(result, "inv-edit-state", "data"))
        assert state["saved"] == [str(CS1)]
        assert "Saved — 1 members" in _text(
            self._single(self._out(result, "inv-edit-message", "children"))
        )
        members = [
            str(sweep)
            for sweep in mounted.shared.detail(mounted.compare).investigation.members
        ]
        assert members == [str(CS1)]
        # the working set stays in the store; no grid echo comes back
        assert "inv-edit-grid" not in json.dumps(result["response"])

    def test_save_on_create_requires_a_complete_body(self, mounted):
        key = self._key(mounted.callback_map, "url.pathname", "inv-edit-save")
        result = self._dispatch(
            mounted,
            key,
            inputs=[
                [
                    {
                        "id": {"inv-edit-save": "save"},
                        "property": "n_clicks",
                        "value": 1,
                    }
                ]
            ],
            state=[
                [
                    {
                        "id": {"inv-edit-state": "members"},
                        "property": "data",
                        "value": self._state([str(CS1)], []),
                    }
                ],
                {"id": "url", "property": "pathname", "value": self._CREATE_ROUTE},
            ],
        )
        assert "required" in _text(self._out(result, "inv-edit-message", "children"))
        assert "url" not in result["response"]

    def test_save_on_create_navigates_to_the_new_investigation(self, mounted):
        key = self._key(mounted.callback_map, "url.pathname", "inv-edit-save")
        result = self._dispatch(
            mounted,
            key,
            inputs=[
                [
                    {
                        "id": {"inv-edit-save": "save"},
                        "property": "n_clicks",
                        "value": 1,
                    }
                ]
            ],
            state=[
                [
                    {
                        "id": {"inv-edit-state": "members"},
                        "property": "data",
                        "value": self._state(
                            [str(CS1), str(CS2)],
                            [],
                            name="fresh-draft",
                            factor="f01",
                            outcome=OUTCOME,
                        ),
                    }
                ],
                {"id": "url", "property": "pathname", "value": self._CREATE_ROUTE},
            ],
        )
        envelope = result["response"]["url"]
        target = envelope["pathname"]
        new_id = target.rsplit("/", 1)[1]
        assert target.startswith(f"{ROUTES_BASE}/project/{CMP}/investigation/")
        members = mounted.shared.detail(new_id).investigation.members
        assert {str(sweep) for sweep in members} == {str(CS1), str(CS2)}

    def test_discard_rolls_the_working_set_back_to_saved(self, mounted):
        key = self._key(mounted.callback_map, "inv-edit-message", "inv-edit-discard")
        result = self._dispatch(
            mounted,
            key,
            inputs=[
                [
                    {
                        "id": {"inv-edit-discard": "discard"},
                        "property": "n_clicks",
                        "value": 1,
                    }
                ]
            ],
            state=[
                [
                    {
                        "id": {"inv-edit-state": "members"},
                        "property": "data",
                        "value": self._state([str(CS1)], [str(CS1), str(CS2)]),
                    }
                ],
                [
                    {
                        "id": {"inv-edit-pick": str(CS1)},
                        "property": "id",
                        "value": {"inv-edit-pick": str(CS1)},
                    },
                    {
                        "id": {"inv-edit-pick": str(CS2)},
                        "property": "id",
                        "value": {"inv-edit-pick": str(CS2)},
                    },
                ],
            ],
        )
        state = self._single(self._out(result, "inv-edit-state", "data"))
        assert state["picked"] == [str(CS1), str(CS2)]
        assert self._out(result, "inv-edit-pick", "value") == [
            [str(CS1)],
            [str(CS2)],
        ]


def _pattern_nodes(component, key: str) -> list:
    return [
        node
        for node in _walk_children(component)
        if isinstance(getattr(node, "id", None), dict) and key in node.id
    ]


def _first_pattern(component, key: str):
    nodes = _pattern_nodes(component, key)
    return nodes[0] if nodes else None


def _sweep_hub(data, compare):
    """(crumb, views row) of the rendered sweep page body."""
    body = sweep_page.render(data, CMP, str(CS1), time.time_ns(), set())
    crumb = next(node for node in body if getattr(node, "className", None) == "crumb")
    views = next(
        node for node in body if getattr(node, "className", None) == "limit-row"
    )
    return crumb, views


def _text(node) -> str:
    """Every scalar leaf concatenated — assertions read facts, not reprs."""
    if isinstance(node, Component):
        return _text(getattr(node, "children", None))
    if isinstance(node, dict):
        return _text((node.get("props") or {}).get("children"))
    if isinstance(node, (list, tuple)):
        return "".join(_text(child) for child in node)
    return "" if node is None else str(node)


def _inv_page(
    cmp_service,
    investigation_id: str,
    view: str = "compare",
    member: str | None = None,
):
    """The investigation page for one plain query string (view and
    member scope included)."""
    search = workspace.investigation_search(view, member)
    return workspace.investigation_page(
        cmp_service,
        CMP,
        investigation_id,
        search=search,
    )


def _string_id_node(page, node_id: str):
    return next(
        (node for node in _walk_children(page) if getattr(node, "id", None) == node_id),
        None,
    )


class TestMemberScopeAndViews:
    """jernerics-g5rw.9: member scope rides the ``view=`` codec with an
    unknown-member fallback, the sweep hub gates its views on real data
    and carries the via return path, and Open in Python exports the
    exact effective membership."""

    def test_member_scope_round_trips_through_the_query_string(self):
        search = workspace.investigation_search("series", str(CS2))
        query = workspace.investigation_query(search)
        assert query["view"] == "series"
        assert query["member"] == str(CS2)
        # Compare is the default; an unknown view name falls back to it.
        assert workspace.investigation_query("")["view"] == "compare"
        assert workspace.investigation_query("?view=bogus")["view"] == "compare"
        # Links compose: the flag and filter ride their own params.
        full = workspace.investigation_query(
            workspace.investigation_search(
                "compare", None, include_invalid=True, q="rmse"
            )
        )
        assert full["include_invalid"] is True
        assert full["q"] == "rmse"

    def test_page_renders_the_scoped_member_fact_and_controls(self, cmp_service, sig):
        page = _inv_page(cmp_service, sig.compare, view="series", member=str(CS2))
        note = _string_id_node(page, "inv-member-note")
        assert _text(note) == "Scoped to member cmp_f02"
        clear = _string_id_node(page, "inv-member-clear")
        assert clear.style == {}
        assert clear.href == (
            f"{ROUTES_BASE}/project/{CMP}/investigation/{sig.compare}?view=series"
        )
        assert _text(page).count("cmp_f02") >= 2  # crumb, h1, and the note

    def test_unknown_member_falls_back_to_all_members(self, cmp_service, sig):
        tray, scoped = investigation_scope_state([str(CS1), str(CS2)], "deadbeef")
        assert scoped is None
        assert tray["sweeps"] == sorted({str(CS1), str(CS2)})
        page = _inv_page(cmp_service, sig.compare, view="points", member="deadbeef")
        note = _string_id_node(page, "inv-member-note")
        assert _text(note) == ""
        clear = _string_id_node(page, "inv-member-clear")
        assert clear.style == {"display": "none"}
        python = _inv_page(cmp_service, sig.compare, view="python", member="deadbeef")
        clipboards = [
            node for node in _walk_children(python) if isinstance(node, dcc.Clipboard)
        ]
        selection = decode_selection(clipboards[0].content)
        assert set(selection.sweeps or ()) == {CS1, CS2, CS3}

    def test_compare_nav_never_carries_a_member_scope(self, cmp_service, sig):
        page = _inv_page(cmp_service, sig.compare, view="series", member=str(CS2))
        seg = _string_id_node(page, "inv-tabs")
        links = {link.children: link.href for link in _of(seg, html.A)}
        assert links["Compare"] == (
            f"{ROUTES_BASE}/project/{CMP}/investigation/{sig.compare}"
        )
        assert f"member={CS2}" in links["Series"]
        assert f"member={CS2}" in links["Search"]
        # Python keeps the scope: its token names the member alone.
        assert f"member={CS2}" in links["Points"]
        python_link = next(
            node.href
            for node in _walk_children(page)
            if isinstance(node, html.A) and node.children == "Open in Python"
        )
        assert f"member={CS2}" in python_link

    def test_page_marks_the_url_view_in_the_nav(self, cmp_service, sig):
        page = _inv_page(cmp_service, sig.compare, view="points", member=str(CS2))
        seg = _string_id_node(page, "inv-tabs")
        marks = {
            link.children: getattr(link, "className", None) for link in _of(seg, html.A)
        }
        assert marks == {
            "Compare": None,
            "Series": None,
            "Points": "on",
            "Search": None,
        }

    def test_points_table_carries_the_member_trials(self, cmp_service, sig):
        tray, _scoped = investigation_scope_state([str(CS1), str(CS2), str(CS3)], None)
        grid = next(
            node
            for node in _walk_children(points_tab(cmp_service, CMP, tray, OUTCOME))
            if isinstance(node, AgGrid)
        )
        assert {row["tk"] for row in grid.rowData} == {
            str(CT1),
            str(CT2),
            str(CT3),
            str(CT4),
        }

    def test_python_token_exports_the_scoped_member_set(self, cmp_service, sig):
        record = cmp_service.investigation_detail(sig.compare).investigation
        scoped = html.Div(workspace.python_body(record, member=str(CS2)))
        clipboards = [
            node for node in _walk_children(scoped) if isinstance(node, dcc.Clipboard)
        ]
        assert decode_selection(clipboards[0].content).sweeps == (CS2,)
        full = html.Div(workspace.python_body(record))
        clipboards = [
            node for node in _walk_children(full) if isinstance(node, dcc.Clipboard)
        ]
        assert set(decode_selection(clipboards[0].content).sweeps or ()) == {
            CS1,
            CS2,
            CS3,
        }
