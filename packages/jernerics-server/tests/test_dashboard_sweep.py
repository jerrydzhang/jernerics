"""The per-sweep page: composition, sub-nav gating, picks, and curation.

Callback-layer coverage over a seeded v3 store: the orchestrator
browser-drives the mounted dashboard after merge, so these tests assert
on the pure page builders the Dash callbacks wrap plus TestClient facts.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from dash import html
from dash.development.base_component import Component
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionEndEvent,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    JobSnapshotEvent,
    SubmissionSnapshotEvent,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    decode_selection,
)
from jernerics_server.dashboard import sweep as sweep_page
from jernerics_server.dashboard import sweep_views
from jernerics_server.dashboard.analysis import (
    default_view_state,
    edited_view,
)
from jernerics_server.dashboard.app import build_dash_app
from jernerics_server.dashboard.artifacts import viewer_href
from jernerics_server.dashboard.auth import DashboardContext
from jernerics_server.dashboard.callbacks import page_content
from jernerics_server.dashboard.routes import ROUTES_BASE
from jernerics_server.dashboard.service import DashboardService
from jernerics_server.dashboard.sessions import SessionSigner
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store

API_KEY = "secret123"
PROJECT = "lab"
WORKSPACE = f"{ROUTES_BASE}/project/{PROJECT}"

SWEEP = uuid.UUID("aa700000-0000-4000-8000-000000000000")
DONE = uuid.UUID("aa710000-0000-4000-8000-000000000000")
T0 = uuid.UUID("cc700000-0000-4000-8000-000000000000")
T1 = uuid.UUID("cc710000-0000-4000-8000-000000000000")
TD = uuid.UUID("cc720000-0000-4000-8000-000000000000")
E0 = uuid.UUID("dd700000-0000-4000-8000-000000000000")
E1 = uuid.UUID("dd710000-0000-4000-8000-000000000000")
ED = uuid.UUID("dd720000-0000-4000-8000-000000000000")
ART = uuid.UUID("ee700000-0000-4000-8000-000000000000")
SUB = uuid.UUID("bb700000-0000-4000-8000-000000000000")
JOB_A = uuid.UUID("fe700000-0000-4000-8000-000000000000")
JOB_B = uuid.UUID("fe710000-0000-4000-8000-000000000000")
SUB = uuid.UUID("bb700000-0000-4000-8000-000000000000")
SUBD = uuid.UUID("bb710000-0000-4000-8000-000000000000")
_BASE = datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC)


def _event(cls, seconds_ago: float, **kwargs):
    return cls(
        event_id=uuid.uuid4(),
        recorded_at=_BASE - timedelta(seconds=seconds_ago),
        **kwargs,
    )


def _seed_events() -> list:
    """One running sweep (a completed trial with params, a value series,
    distributions, and an artifact, plus a running trial) and one bare
    completed sweep with no distributions and no values."""
    return [
        _event(
            SweepSnapshotEvent,
            1000,
            project=PROJECT,
            sweep_id=SWEEP,
            name="grid-search",
            state="running",
        ),
        _event(
            SubmissionSnapshotEvent,
            995,
            submission_id=SUB,
            sweep_id=SWEEP,
            backend="slurm",
            state="running",
            submitted_at=_BASE - timedelta(seconds=995),
            expected_trials=8,
            git_hash="deadbeefdeadbeefdeadbeefdeadbeef",
            config_source="experiments/grid.py",
        ),
        _event(
            JobSnapshotEvent,
            994,
            job_id=JOB_A,
            submission_id=SUB,
            scheduler_job_id="9400001",
            role="trials",
            state="running",
        ),
        _event(
            JobSnapshotEvent,
            993,
            job_id=JOB_B,
            submission_id=SUB,
            scheduler_job_id="9400002",
            role="checker",
            state="running",
        ),
        _event(
            TrialSnapshotEvent,
            990,
            trial_id=T0,
            sweep_id=SWEEP,
            number=0,
            state=TrialState.COMPLETED,
            retry_root_trial_id=T0,
            objective=0.5,
            params=FlatContext({"lr": 0.1}),
            distributions=FlatContext({"lr": "uniform(0, 1)"}),
        ),
        _event(
            TrialSnapshotEvent,
            989,
            trial_id=T1,
            sweep_id=SWEEP,
            number=1,
            state=TrialState.RUNNING,
            retry_root_trial_id=T1,
            params=FlatContext({"lr": 0.2}),
            distributions=FlatContext({"lr": "uniform(0, 1)"}),
        ),
        _event(
            ExecutionStartEvent,
            985,
            execution_id=E0,
            trial_id=T0,
            hostname="node00",
            started_at=_BASE - timedelta(seconds=985),
        ),
        _event(ValueEvent, 980, trial_id=T0, key="loss", step=0, value=0.9),
        _event(ValueEvent, 979, trial_id=T0, key="loss", step=1, value=0.8),
        _event(
            ExecutionEndEvent,
            978,
            execution_id=E0,
            ended_at=_BASE - timedelta(seconds=978),
            outcome="success",
            exit_code=0,
        ),
        _event(
            ExecutionStartEvent,
            970,
            execution_id=E1,
            trial_id=T1,
            hostname="node01",
            started_at=_BASE - timedelta(seconds=970),
        ),
        _event(
            ArtifactDeclarationEvent,
            960,
            artifact_id=ART,
            trial_id=T0,
            key="model",
            filename="model.bin",
            content_type="application/octet-stream",
            size_bytes=3,
        ),
        _event(
            SweepSnapshotEvent,
            500,
            project=PROJECT,
            sweep_id=DONE,
            name="done-sweep",
            state="completed",
        ),
        _event(
            SubmissionSnapshotEvent,
            495,
            submission_id=SUBD,
            sweep_id=DONE,
            backend="local",
            state="completed",
            submitted_at=_BASE - timedelta(seconds=495),
        ),
        _event(
            TrialSnapshotEvent,
            490,
            trial_id=TD,
            sweep_id=DONE,
            number=0,
            state=TrialState.COMPLETED,
            retry_root_trial_id=TD,
            objective=1.0,
        ),
        _event(
            ExecutionStartEvent,
            485,
            execution_id=ED,
            trial_id=TD,
            hostname="node09",
            started_at=_BASE - timedelta(seconds=485),
        ),
        _event(
            SubmissionSnapshotEvent,
            495,
            submission_id=SUBD,
            sweep_id=DONE,
            backend="local",
            state="completed",
            submitted_at=_BASE - timedelta(seconds=495),
        ),
        _event(
            ExecutionEndEvent,
            480,
            execution_id=ED,
            ended_at=_BASE - timedelta(seconds=480),
            outcome="success",
            exit_code=0,
        ),
    ]


def _seeded_store(path) -> Store:
    store = Store(path / "sweep-page.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_seed_events())
    )
    assert not result.conflicts
    return store


@pytest.fixture
def service(tmp_path):
    store = _seeded_store(tmp_path)
    return DashboardService(QueryService(store), store)


@pytest.fixture
def investigation_id(service):
    record = service.create_investigation(
        PROJECT, "roberts", "lr", "final_loss", members=[str(SWEEP)]
    )
    return str(record.id)


def _render(service, sweep_id, via=None, picks=(), now=0):
    data = sweep_page.collect(service, str(sweep_id), via)
    assert data is not None
    return sweep_page.render(data, PROJECT, str(sweep_id), now, set(picks))


def _walk(component):
    if isinstance(component, list | tuple):
        for item in component:
            yield from _walk(item)
        return
    yield component
    children = getattr(component, "children", None)
    if isinstance(children, Component):
        yield from _walk(children)
    elif isinstance(children, list | tuple):
        for child in children:
            if isinstance(child, Component):
                yield from _walk(child)


def _text(nodes) -> str:
    if isinstance(nodes, (list, tuple)):
        return "".join(_text(node) for node in nodes)
    if not isinstance(nodes, Component):
        return str(nodes)
    return _text(getattr(nodes, "children", None))


def _flat(node) -> str:
    """Ids, classes, and leaf text in one string — assertions read the
    whole tree, never Dash's truncated repr."""
    parts: list[str] = []
    for item in _walk(node):
        ident = getattr(item, "id", None)
        if ident is not None:
            parts.append(str(ident))
        cls = getattr(item, "className", None)
        if cls:
            parts.append(str(cls))
        parts.append(_text(item))
    return " ".join(parts)


def _seg_labels(body) -> list[str]:
    row = next(node for node in body if getattr(node, "className", None) == "limit-row")
    seg = next(
        node
        for node in _walk(row)
        if isinstance(node, html.Div) and getattr(node, "className", None) == "seg"
    )
    return [
        node.children for node in _walk(seg) if isinstance(node, (html.A, html.Span))
    ]


def _seg_hrefs(body) -> dict[str, str]:
    row = next(node for node in body if getattr(node, "className", None) == "limit-row")
    return {
        node.children: str(getattr(node, "href", None))
        for node in _walk(row)
        if isinstance(node, html.A) and getattr(node, "href", None)
    }


class TestPageComposition:
    def test_route_renders_the_prototype_sections(self, service):
        page, polls = page_content(f"{WORKSPACE}/sweep/{SWEEP}", service)
        rendered = _flat(page)
        assert "grid-search" in rendered
        assert "st-running" in rendered
        for section in ("Executions", "Trials", "Params"):
            assert section in rendered
        assert "meta-grid" in rendered
        assert "selbox" in rendered
        assert "9400001" in rendered and "checker" in rendered
        assert polls is True

    def test_completed_sweep_stops_polling(self, service):
        _page, polls = page_content(f"{WORKSPACE}/sweep/{DONE}", service)
        assert polls is False

    def test_bare_sweep_crumb_has_no_investigation_hops(self, service):
        body = _render(service, SWEEP)
        crumb = _text(
            next(node for node in body if getattr(node, "className", None) == "crumb")
        )
        assert crumb.startswith(f"Projects›{PROJECT}")
        assert "Investigations" not in crumb

    def test_artifact_chips_open_the_viewer(self, service):
        body = _render(service, SWEEP)
        chips = [
            node
            for node in _walk(body)
            if isinstance(node, html.A)
            and "/artifact-view/" in str(getattr(node, "href", None))
        ]
        assert [node.children for node in chips] == ["model"]
        assert chips[0].href == viewer_href(str(ART))
        assert getattr(chips[0], "title", None) == "model.bin"

    def test_params_table_aggregates_distinct_values(self, service):
        rendered = _flat(_render(service, SWEEP))
        assert "sampled" in rendered
        assert "0.1, 0.2" in rendered


class TestSubNavAvailability:
    def test_without_via_supported_views_link_the_sweep_sub_views(
        self, service, investigation_id
    ):
        body = _render(service, SWEEP)
        assert _seg_labels(body) == [
            "Overview",
            "Series",
            "Points",
            "Search",
            "Optuna",
        ]
        hrefs = _seg_hrefs(body)
        assert set(hrefs) == {"Series", "Points", "Search", "Optuna"}
        for view in ("series", "points", "search", "optuna"):
            assert hrefs[view.capitalize()] == sweep_views.sweep_href(
                PROJECT, str(SWEEP), view
            )

    def test_via_keeps_member_scoped_investigation_destinations(
        self, service, investigation_id
    ):
        body = _render(service, SWEEP, via=investigation_id)
        assert _seg_labels(body) == [
            "Overview",
            "Series",
            "Points",
            "Search",
            "Optuna",
        ]
        hrefs = _seg_hrefs(body)
        for view in ("Series", "Points"):
            assert f"view={view.lower()}&member={SWEEP}" in hrefs[view]
        assert "view=search" in hrefs["Search"]
        assert "member=" not in hrefs["Search"]
        assert hrefs["Optuna"] == sweep_views.sweep_href(PROJECT, str(SWEEP), "optuna")

    def test_sub_nav_marks_the_active_view(self, service):
        data = sweep_page.collect(service, str(SWEEP), None)
        assert data is not None
        row = sweep_page._views_row(PROJECT, str(SWEEP), data, "series")
        links = [
            node
            for node in _walk(row)
            if isinstance(node, html.A) and getattr(node, "href", None)
        ]
        assert [
            str(node.children)
            for node in links
            if getattr(node, "className", None) == "on"
        ] == ["Series"]

    def test_series_needs_step_series_and_optuna_needs_distributions(self, service):
        body = _render(service, DONE)
        assert _seg_labels(body) == ["Overview", "Points", "Search"]
        assert _seg_hrefs(body) == {
            "Points": sweep_views.sweep_href(PROJECT, str(DONE), "points"),
            "Search": sweep_views.sweep_href(PROJECT, str(DONE), "search"),
        }


class TestSubViews:
    def test_series_renders_blocks_chips_and_pcp(self, service):
        page, polls = page_content(
            f"{WORKSPACE}/sweep/{SWEEP}", service, search="?view=series"
        )
        rendered = _flat(page)
        assert "sweep-series-blocks" in rendered
        assert "loss" in rendered
        assert "sweep-series-display" in rendered
        assert "sweep-series-scale" in rendered
        assert "sweep-series-row" in rendered
        assert polls is True

    def test_points_renders_grid_and_parcoords(self, service):
        page, polls = page_content(
            f"{WORKSPACE}/sweep/{SWEEP}", service, search="?view=points"
        )
        rendered = _flat(page)
        assert "sweep-points-grid" in rendered
        assert "sweep-points-figure" in rendered
        assert "objective (final)" in rendered
        assert polls is True

    def test_search_filters_this_sweeps_trials(self, service):
        page, polls = page_content(
            f"{WORKSPACE}/sweep/{SWEEP}", service, search="?view=search"
        )
        rendered = _flat(page)
        assert "sweep-search-q" in rendered
        assert "2 of 2 trials" in rendered
        data = sweep_views.search_data_fetch(service, PROJECT, str(SWEEP), 0)
        rows = sweep_views.search_rows(data, "0.1")
        assert len(rows) == 1
        assert polls is True

    def test_optuna_renders_the_study_figures(self, service):
        page, polls = page_content(
            f"{WORKSPACE}/sweep/{SWEEP}", service, search="?view=optuna"
        )
        rendered = _flat(page)
        for section in (
            "Objective history",
            "Params → objective",
            "Parameter slices",
            "Objective contour",
            "Trial timeline",
        ):
            assert section in rendered
        assert polls is True

    def test_optuna_contour_needs_two_distinct_numeric_params(self, service):
        data = service.analysis_trials(PROJECT, sweep_views.sweep_tray(str(SWEEP)))
        for y_key in (None, "lr"):
            figure = sweep_views.contour_figure(data, "lr", y_key)
            assert "contour needs" in str(figure.layout.title)

    def test_python_disclosure_carries_the_sweep_token(self, service):
        page, _polls = page_content(f"{WORKSPACE}/sweep/{SWEEP}", service)
        pres = [
            node
            for node in _walk(page)
            if isinstance(node, html.Pre) and "config-json" in str(node.className)
        ]
        token = str(pres[0].children)
        selection = decode_selection(token)
        assert selection.project == PROJECT
        assert selection.sweeps == (SWEEP,)

    def test_unsupported_view_falls_back_to_the_overview(self, service):
        page, polls = page_content(
            f"{WORKSPACE}/sweep/{DONE}", service, search="?view=optuna"
        )
        rendered = _flat(page)
        assert "Executions" in rendered and "Trials" in rendered
        assert "sweep-optuna-history" not in rendered
        assert polls is False


class TestRetryRootPicking:
    def test_checkboxes_reflect_the_scope_families(self, service):
        doc = edited_view(
            default_view_state(), {"scope": {"sweeps": [], "families": [str(T0)]}}
        )
        page, _polls = page_content(f"{WORKSPACE}/sweep/{SWEEP}", service, view_doc=doc)
        checklists = [
            node
            for node in _walk(page)
            if isinstance(getattr(node, "id", None), dict)
            and "sweep-trial-pick" in node.id
        ]
        assert [(node.id["sweep-trial-pick"], node.value) for node in checklists] == [
            (0, [str(T0)]),
            (1, []),
        ]

    def test_picking_writes_the_scope_and_mount_echo_is_a_no_op(
        self, authed, callback_map
    ):
        client, _store = authed
        key = _callback_key(callback_map)
        url = f"{WORKSPACE}/sweep/{SWEEP}"

        def fire(values, current=None):
            return _fire_picks(client, key, url, values, current)

        response, payload = fire([[str(T0)], []])
        assert response.status_code == 200
        scope = payload["view-store"]["data"]["scope"]
        assert scope["families"] == [str(T0)]
        # The echo of the just-written state, seen from a store that
        # already holds it, writes nothing.
        written = payload["view-store"]["data"]
        echo = fire([[str(T0)], []], current=written)
        assert echo[0].status_code == 204
        # Checking the second row merges its root; unchecking clears.
        both = fire([[str(T0)], [str(T1)]], current=written)
        assert both[1]["view-store"]["data"]["scope"]["families"] == [
            str(T0),
            str(T1),
        ]
        cleared = fire([[], []], current=both[1]["view-store"]["data"])
        assert cleared[1]["view-store"]["data"]["scope"]["families"] == []


class TestCurationSurface:
    def test_archived_page_offers_unarchive(self, service):
        service.archive_sweep(str(DONE))
        body = _render(service, DONE)
        buttons = {
            node.children
            for node in _walk(body)
            if isinstance(node, html.Button) and isinstance(node.children, str)
        }
        assert "Unarchive" in buttons
        assert "Archive" not in buttons
        heading = _text(next(node for node in body if isinstance(node, html.H1)))
        assert "archived" in heading

    def test_invalid_page_carries_the_reason(self, service):
        service.mark_sweep_invalid(str(DONE), "contaminated dataset")
        body = _render(service, DONE)
        heading = _text(next(node for node in body if isinstance(node, html.H1)))
        assert "invalid" in heading
        assert "reason: contaminated dataset" in heading


# -- Mounted-callback dispatch machinery (mirrors test_dashboard_poll) ----


@pytest.fixture
def authed(tmp_path):
    store = _seeded_store(tmp_path)
    client = TestClient(
        create_app(
            store,
            api_key=API_KEY,
            artifacts_root=tmp_path / "artifacts",
            dashboard=True,
        ),
        base_url="https://testserver",
    )
    response = client.post(
        "/dashboard/login", data={"api_key": API_KEY}, follow_redirects=False
    )
    assert response.status_code == 303
    return client, store


@pytest.fixture(scope="class")
def callback_map(tmp_path_factory):
    service = DashboardService(
        QueryService(_seeded_store(tmp_path_factory.mktemp("sweep-page-graph")))
    )
    ctx = DashboardContext(
        api_key=API_KEY,
        queries=service.queries,
        service=service,
        signer=SessionSigner(b"\x00" * 32),
    )
    return build_dash_app(ctx).callback_map


def _outputs_of(key: str) -> set[str]:
    stripped = key.removeprefix("..").removesuffix("..")
    return {part.split("@")[0] for part in stripped.split("...") if part}


def _callback_key(callback_map) -> str:
    """The retry-root picker: the only callback fed solely by the
    sweep-trial-pick checklists."""
    return next(
        key
        for key in callback_map
        if _outputs_of(key) == {"view-store.data"}
        and {spec["id"] for spec in callback_map[key]["inputs"]}
        == {'{"sweep-trial-pick":["ALL"]}'}
    )


def _fire_picks(client, key, url, values, current=None):
    inputs = [
        [
            {
                "id": {"sweep-trial-pick": index},
                "property": "value",
                "value": value,
            }
            for index, value in enumerate(values)
        ]
    ]
    response = client.post(
        "/dashboard/_dash-update-component",
        json={
            "output": key,
            "outputs": [{"id": "view-store", "property": "data"}],
            "inputs": inputs,
            "state": [
                {"id": "view-store", "property": "data", "value": current},
                {"id": "url", "property": "pathname", "value": url},
            ],
            "changedPropIds": [
                f'{{"sweep-trial-pick": {index}}}.value' for index in range(len(values))
            ],
        },
    )
    payload = response.json()["response"] if response.status_code == 200 else None
    return response, payload
