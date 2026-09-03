"""DashboardService investigation reads, scope materialization, writes."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionEndEvent,
    ExecutionStartEvent,
    IngestRequest,
    Selection,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    encode_selection,
    materialize_selection,
)
from jernerics_server.dashboard.analysis import (
    EMPTY_TRAY,
    default_scope_state,
    tray_from_selection,
)
from jernerics_server.dashboard.selection_tokens import decode_selection_token
from jernerics_server.dashboard.service import (
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
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
        assert lone.state == "completed"
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
        scope = service.investigation_scope(compare)
        assert scope["sweeps"] == sorted(
            str(sweep_id) for sweep_id in (VAL_SWEEP, INC_SWEEP, BAD_SWEEP)
        )
        assert scope["trials"] == []
        assert scope["families"] == []
        assert scope["executions"] == []
        assert scope["expand"] is False
        assert scope["include_archived"] is False
        assert scope["include_invalid"] is False
        assert set(scope) == set(default_scope_state())

    def test_url_token_round_trips_to_the_same_scope(self, service, shared, compare):
        record = shared.detail(compare).investigation
        token = encode_selection(materialize_selection(record))
        selection = decode_selection_token(token, project=LAB)
        tray = tray_from_selection(selection)
        scope = service.investigation_scope(compare)
        assert {key: scope[key] for key in EMPTY_TRAY} == tray

    def test_member_index_narrows_to_one_sweep(self, service, compare):
        scope = service.investigation_scope(compare, member_index=1)
        assert scope["sweeps"] == [str(INC_SWEEP)]
        assert scope["trials"] == []
        assert service.investigation_scope(compare, member_index=0)["sweeps"] == [
            str(VAL_SWEEP)
        ]

    def test_out_of_range_member_index_is_an_error(self, service, shared, compare):
        with pytest.raises(IndexError, match="member index 3"):
            service.investigation_scope(compare, member_index=3)
        empty = str(shared.detail_by_name(LAB, "gamma-empty").investigation.id)
        with pytest.raises(IndexError, match="member index 0"):
            service.investigation_scope(empty, member_index=0)


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
