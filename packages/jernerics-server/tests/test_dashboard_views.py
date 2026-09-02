"""Workspace browser, inspector, and curation coverage.

Callback-layer coverage over a seeded v3 store: the orchestrator
browser-drives the mounted dashboard after merge, so these tests assert
on the pure helpers the Dash callbacks wrap plus TestClient page 200s.
"""

import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dash import dcc, html
from dash.development.base_component import Component
from dash_ag_grid import AgGrid
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionOutcome,
    ExecutionProgressEvent,
    ExecutionRecord,
    ExecutionStartEvent,
    FailureKind,
    FlatContext,
    IngestRequest,
    JobSnapshotEvent,
    ManualParamEvent,
    SubmissionSnapshotEvent,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
)
from jernerics_server.dashboard import layout
from jernerics_server.dashboard.analysis import (
    origin_from_href,
    python_snippet,
    python_tab,
    tray_summary,
)
from jernerics_server.dashboard.callbacks import (
    apply_curation,
    lineage_panel,
    page_content,
    remember_workspace,
    selected_failed_sweeps,
    sort_from_columns,
    tray_from_grid,
    workspace_state,
)
from jernerics_server.dashboard.components import (
    TEXT_LIMIT,
    absolute_time,
    datetime_to_ns,
    grid_options,
    short_id,
)
from jernerics_server.dashboard.service import (
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
)
from jernerics_server.dashboard.workspace import (
    _MONITORING_ORDER,
    _executions_table,
    _monitoring_badges,
    _monitoring_counts,
    browser_sweep_rows,
    curation_note,
    curation_transitions,
    detail_curation,
    failed_view_panel,
    family_grid_row,
    inspector_content,
    overview_rollup,
    overview_tab,
    scoped_sweeps,
    selection_transitions,
)
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store

API_KEY = "secret123"

SWEEP_A = uuid.UUID("aa110000-0000-4000-8000-000000000000")
SWEEP_B = uuid.UUID("aa220000-0000-4000-8000-000000000000")
SUB_A = uuid.UUID("bb110000-0000-4000-8000-000000000000")
SUB_B = uuid.UUID("bb120000-0000-4000-8000-000000000000")
JOB_A1 = uuid.UUID("ee110000-0000-4000-8000-000000000000")
JOB_A2 = uuid.UUID("ee120000-0000-4000-8000-000000000000")
F0 = uuid.UUID("cc110000-0000-4000-8000-000000000000")
F1 = uuid.UUID("cc120000-0000-4000-8000-000000000000")
F2 = uuid.UUID("cc130000-0000-4000-8000-000000000000")
T4 = uuid.UUID("cc140000-0000-4000-8000-000000000000")
T5 = uuid.UUID("cc150000-0000-4000-8000-000000000000")
T6 = uuid.UUID("cc160000-0000-4000-8000-000000000000")
T7 = uuid.UUID("cc170000-0000-4000-8000-000000000000")
T8 = uuid.UUID("cc180000-0000-4000-8000-000000000000")
T9 = uuid.UUID("cc190000-0000-4000-8000-000000000000")
E1 = uuid.UUID("dd110000-0000-4000-8000-000000000000")
E3 = uuid.UUID("dd130000-0000-4000-8000-000000000000")
E4 = uuid.UUID("dd140000-0000-4000-8000-000000000000")
E5 = uuid.UUID("dd150000-0000-4000-8000-000000000000")
E6 = uuid.UUID("dd160000-0000-4000-8000-000000000000")
E7 = uuid.UUID("dd170000-0000-4000-8000-000000000000")
E8 = uuid.UUID("dd180000-0000-4000-8000-000000000000")
CUR_SWEEP_OLD = uuid.UUID("aa310000-0000-4000-8000-000000000000")
CUR_SWEEP_NEW = uuid.UUID("aa320000-0000-4000-8000-000000000000")
CUR_SWEEP_DONE = uuid.UUID("aa330000-0000-4000-8000-000000000000")
CUR_T1 = uuid.UUID("cc310000-0000-4000-8000-000000000000")
CUR_T2 = uuid.UUID("cc320000-0000-4000-8000-000000000000")
CUR_T3 = uuid.UUID("cc330000-0000-4000-8000-000000000000")
CUR_E1 = uuid.UUID("dd310000-0000-4000-8000-000000000000")
CUR_E2 = uuid.UUID("dd320000-0000-4000-8000-000000000000")
CUR_E3 = uuid.UUID("dd330000-0000-4000-8000-000000000000")


def _seed_events() -> list:
    """A project with two sweeps. Sweep A: one submission with an array
    job and a checker job, a three-generation retry family, executions in
    every monitoring state, explicit progress, a resolved_config JSON
    observation, and full provenance. Sweep B: terminal only."""
    now = datetime.now(UTC)

    def at(seconds_ago: float) -> datetime:
        return now - timedelta(seconds=seconds_ago)

    def event(cls, seconds_ago: float, **kwargs):
        return cls(event_id=uuid.uuid4(), recorded_at=at(seconds_ago), **kwargs)

    return [
        event(
            SweepSnapshotEvent,
            650,
            project="ops",
            sweep_id=SWEEP_A,
            name="alpha",
            state="running",
        ),
        event(
            SweepSnapshotEvent,
            95,
            project="ops",
            sweep_id=SWEEP_A,
            name="alpha",
            state="running",
        ),
        event(
            SweepSnapshotEvent,
            150,
            project="ops",
            sweep_id=SWEEP_B,
            name="beta",
            state="completed",
        ),
        event(
            SubmissionSnapshotEvent,
            640,
            submission_id=SUB_A,
            sweep_id=SWEEP_A,
            backend="slurm",
            state="running",
            submitted_at=at(650),
            expected_trials=8,
            git_hash="deadbeef",
            config_source="sweep.yaml",
        ),
        event(
            JobSnapshotEvent,
            639,
            job_id=JOB_A1,
            submission_id=SUB_A,
            scheduler_job_id="9400001",
            role="trials",
            state="running",
        ),
        event(
            JobSnapshotEvent,
            639,
            job_id=JOB_A2,
            submission_id=SUB_A,
            scheduler_job_id="9400002",
            role="checker",
            state="running",
        ),
        event(
            SubmissionSnapshotEvent,
            260,
            submission_id=SUB_B,
            sweep_id=SWEEP_B,
            backend="local",
            state="completed",
            submitted_at=at(260),
            expected_trials=1,
        ),
        event(
            TrialSnapshotEvent,
            630,
            trial_id=F0,
            sweep_id=SWEEP_A,
            number=1,
            state=TrialState.FAILED,
            retry_root_trial_id=F0,
        ),
        event(
            TrialSnapshotEvent,
            620,
            trial_id=F1,
            sweep_id=SWEEP_A,
            number=2,
            state=TrialState.FAILED,
            retry_of_trial_id=F0,
            retry_root_trial_id=F0,
            retry_index=1,
        ),
        event(
            TrialSnapshotEvent,
            110,
            trial_id=F2,
            sweep_id=SWEEP_A,
            number=3,
            state=TrialState.COMPLETED,
            retry_of_trial_id=F1,
            retry_root_trial_id=F0,
            retry_index=2,
            objective=0.75,
            params=FlatContext({"lr": 0.1, "batch": 32, "depth": 4}),
        ),
        event(ManualParamEvent, 109, trial_id=F2, key="seed", value=17),
        event(
            TrialSnapshotEvent,
            70,
            trial_id=T4,
            sweep_id=SWEEP_A,
            number=4,
            state=TrialState.RUNNING,
            retry_root_trial_id=T4,
        ),
        event(
            TrialSnapshotEvent,
            610,
            trial_id=T5,
            sweep_id=SWEEP_A,
            number=5,
            state=TrialState.RUNNING,
            retry_root_trial_id=T5,
        ),
        event(
            TrialSnapshotEvent,
            1510,
            trial_id=T6,
            sweep_id=SWEEP_A,
            number=6,
            state=TrialState.RUNNING,
            retry_root_trial_id=T6,
        ),
        event(
            TrialSnapshotEvent,
            160,
            trial_id=T7,
            sweep_id=SWEEP_A,
            number=7,
            state=TrialState.WAITING,
            retry_root_trial_id=T7,
        ),
        event(
            TrialSnapshotEvent,
            410,
            trial_id=T8,
            sweep_id=SWEEP_A,
            number=8,
            state=TrialState.RUNNING,
            retry_root_trial_id=T8,
        ),
        event(
            TrialSnapshotEvent,
            250,
            trial_id=T9,
            sweep_id=SWEEP_B,
            number=1,
            state=TrialState.COMPLETED,
            retry_root_trial_id=T9,
            objective=0.25,
        ),
        event(
            ExecutionStartEvent,
            700,
            execution_id=E1,
            trial_id=F0,
            hostname="node01",
            started_at=at(700),
        ),
        event(ExecutionHeartbeatEvent, 695, execution_id=E1, at=at(695)),
        event(
            ExecutionEndEvent,
            600,
            execution_id=E1,
            ended_at=at(600),
            outcome="failure",
            exit_code=1,
            failure_kind="exception",
            failure_summary="boom: divide by zero",
        ),
        event(
            ExecutionStartEvent,
            300,
            execution_id=E3,
            trial_id=F2,
            hostname="node02",
            started_at=at(300),
        ),
        event(ExecutionHeartbeatEvent, 150, execution_id=E3, at=at(150)),
        event(ValueEvent, 140, trial_id=F2, key="loss", step=0, value=0.3),
        event(
            ExecutionEndEvent,
            100,
            execution_id=E3,
            ended_at=at(100),
            outcome="success",
            exit_code=0,
        ),
        event(
            ExecutionStartEvent,
            70,
            execution_id=E4,
            trial_id=T4,
            hostname="node03",
            started_at=at(70),
        ),
        event(ExecutionHeartbeatEvent, 60, execution_id=E4, at=at(60)),
        event(
            ExecutionProgressEvent,
            65,
            execution_id=E4,
            current=7,
            total=10,
            unit="epoch",
        ),
        event(
            ValueEvent,
            62,
            trial_id=T4,
            key="resolved_config",
            step=0,
            observation={"lr": 0.1, "batch": 32, "notes": "seed run"},
        ),
        event(ValueEvent, 61, trial_id=T4, key="loss", step=1, value=0.4),
        event(
            ExecutionStartEvent,
            600,
            execution_id=E5,
            trial_id=T5,
            hostname="node04",
            started_at=at(600),
        ),
        event(ExecutionHeartbeatEvent, 500, execution_id=E5, at=at(500)),
        event(
            ExecutionProgressEvent,
            550,
            execution_id=E5,
            current=3,
            total=10,
            unit="epoch",
        ),
        event(
            ExecutionStartEvent,
            1500,
            execution_id=E6,
            trial_id=T6,
            hostname="node05",
            started_at=at(1500),
        ),
        event(ExecutionHeartbeatEvent, 1200, execution_id=E6, at=at(1200)),
        event(
            ExecutionStartEvent,
            400,
            execution_id=E7,
            trial_id=T8,
            hostname="node06",
            started_at=at(400),
        ),
        event(
            ExecutionStartEvent,
            200,
            execution_id=E8,
            trial_id=T9,
            hostname="node07",
            started_at=at(200),
        ),
        event(ExecutionHeartbeatEvent, 160, execution_id=E8, at=at(160)),
        event(
            ExecutionEndEvent,
            150,
            execution_id=E8,
            ended_at=at(150),
            outcome="success",
            exit_code=0,
        ),
    ]


def _curation_seed_events() -> list:
    """Project curate: terminal sweeps older/newer; project done: one
    terminal sweep. Flat, terminal-only facts for curation reads."""
    now = datetime.now(UTC)

    def at(seconds_ago: float) -> datetime:
        return now - timedelta(seconds=seconds_ago)

    def event(cls, seconds_ago: float, **kwargs):
        return cls(event_id=uuid.uuid4(), recorded_at=at(seconds_ago), **kwargs)

    def terminal_sweep(
        sweep_id: uuid.UUID,
        project: str,
        name: str,
        trial_id: uuid.UUID,
        execution_id: uuid.UUID,
        seconds_ago: float,
    ) -> list:
        return [
            event(
                SweepSnapshotEvent,
                seconds_ago,
                project=project,
                sweep_id=sweep_id,
                name=name,
                state="completed",
            ),
            event(
                TrialSnapshotEvent,
                seconds_ago - 10,
                trial_id=trial_id,
                sweep_id=sweep_id,
                number=0,
                state=TrialState.COMPLETED,
                retry_root_trial_id=trial_id,
            ),
            event(
                ExecutionStartEvent,
                seconds_ago - 20,
                execution_id=execution_id,
                trial_id=trial_id,
                hostname="node09",
                started_at=at(seconds_ago - 20),
            ),
            event(
                ExecutionEndEvent,
                seconds_ago - 30,
                execution_id=execution_id,
                ended_at=at(seconds_ago - 30),
                outcome="success",
                exit_code=0,
            ),
        ]

    return [
        *terminal_sweep(CUR_SWEEP_OLD, "curate", "older", CUR_T1, CUR_E1, 300),
        *terminal_sweep(CUR_SWEEP_NEW, "curate", "newer", CUR_T2, CUR_E2, 100),
        *terminal_sweep(CUR_SWEEP_DONE, "done", "only", CUR_T3, CUR_E3, 50),
    ]


@pytest.fixture
def store_and_service(tmp_path) -> tuple[Store, DashboardService]:
    store = Store(tmp_path / "views.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_seed_events())
    )
    assert not result.conflicts
    return store, DashboardService(QueryService(store))


@pytest.fixture
def service(store_and_service) -> DashboardService:
    return store_and_service[1]


@pytest.fixture
def curated(tmp_path) -> tuple[Store, DashboardService]:
    store = Store(tmp_path / "curation.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_curation_seed_events())
    )
    assert not result.conflicts
    return store, DashboardService(QueryService(store))


@pytest.fixture
def authed(tmp_path) -> TestClient:
    store = Store(tmp_path / "views.sqlite")
    IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_seed_events())
    )
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
    return client


@pytest.fixture
def mutable(tmp_path) -> tuple[Store, DashboardService]:
    store = Store(tmp_path / "mutable.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_seed_events())
    )
    assert not result.conflicts
    return store, DashboardService(QueryService(store), store)


@pytest.fixture
def mutable_client(tmp_path) -> tuple[Store, TestClient]:
    store = Store(tmp_path / "mounted.sqlite")
    IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_seed_events())
    )
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
    return store, client


def _walk(component: Component):
    yield component
    children = getattr(component, "children", None)
    if isinstance(children, Component):
        yield from _walk(children)
    elif isinstance(children, list | tuple):
        for child in children:
            if isinstance(child, Component):
                yield from _walk(child)


def _grid(page: Any, grid_id: str | dict[str, str]) -> Any:
    found = [
        node for node in _walk(page) if isinstance(node, AgGrid) and node.id == grid_id
    ]
    assert found, f"{grid_id} missing from page"
    return found[0]


def _inspector(service: DashboardService, kind: str, object_id) -> Any:
    return inspector_content(service, {"kind": kind, "id": str(object_id)}, 0)


def _focus_ref(kind: str, object_id) -> str:
    return f"{{'focus-object': '{kind}:{object_id}'}}"


NOW = 0


class TestProjectsPage:
    def test_counts_recent_sweep_and_relative_activity(self, service):
        catalog = service.project_catalog()
        assert len(catalog) == 1
        row = catalog[0]
        assert row.project == "ops"
        assert row.active == 1
        assert row.stale == 1
        assert row.failed == 1
        assert row.succeeded == 2
        assert row.recent_sweep == "alpha"

        page, polls = page_content("/dashboard/", service)
        rendered = str(page)
        assert "ops" in rendered
        assert "active 1" in rendered
        assert "stale 1" in rendered
        assert "failed 1" in rendered
        assert "alpha" in rendered
        assert "1m ago" in rendered
        assert "/dashboard/project/ops" in rendered
        assert polls is False


class TestWorkspaceLayout:
    def test_workspace_mounts_browser_tabs_and_inspector_once(self, service):
        page, polls = page_content("/dashboard/project/ops", service)
        rendered = str(page)
        assert "Project ops" in rendered
        assert "id='scope-browser'" in rendered
        assert "id='sweep-grid'" in rendered
        assert "id='analysis-family-grid'" in rendered
        assert "id='analysis-expand'" in rendered
        assert "id='analysis-include'" in rendered
        assert "id='workspace-quick'" in rendered
        assert "id='inspector'" in rendered
        assert "id='workspace-overview'" in rendered
        for tab in ("overview", "catalog", "series", "points", "optuna", "python"):
            assert f"value='{tab}'" in rendered
        for button in ("ws-archive", "ws-invalid", "ws-restore-validity", "ws-restore"):
            assert f"id='{button}'" in rendered
        assert "id='ws-reason'" in rendered
        assert "id='workspace-message'" in rendered
        assert "id='workspace-curation-note'" in rendered
        assert polls is True  # alpha is incomplete

    def test_curation_panel_collapsed_by_default_with_gated_reason(self, service):
        page, _ = page_content("/dashboard/project/ops", service)
        panel = next(
            node
            for node in _walk(page)
            if getattr(node, "id", None) == "curation-panel"
        )
        assert panel.className == "curation-panel"
        assert not getattr(panel, "open", False)  # collapsed until asked for
        summary = next(
            node
            for node in _walk(panel)
            if getattr(node, "id", None) == "ws-curation-summary"
        )
        assert summary.children == "Curation…"
        reason = next(
            node for node in _walk(panel) if getattr(node, "id", None) == "ws-reason"
        )
        assert reason.style == {"display": "none"}  # no permanently empty input
        message = next(
            node
            for node in _walk(panel)
            if getattr(node, "id", None) == "workspace-message"
        )
        assert getattr(message, "children", None) is None
        panel_ids = {getattr(node, "id", None) for node in _walk(panel)}
        # The active-work warning stays outside the collapsed panel.
        assert "workspace-curation-note" not in panel_ids
        assert any(
            getattr(node, "id", None) == "workspace-curation-note"
            for node in _walk(page)
        )

    def test_browser_grid_has_stable_row_ids(self, service):
        page, _ = page_content("/dashboard/project/ops", service)
        sweep_grid = _grid(page, "sweep-grid")
        assert sweep_grid.getRowId == "params.data.sweep_id"
        family_grid = _grid(page, "analysis-family-grid")
        assert family_grid.getRowId == "params.data.root || params.data.trial_id"

    def test_browser_rows_carry_operational_facts(self, service):
        rows = {
            row["sweep_id"]: row
            for row in browser_sweep_rows(service.sweep_overview("ops"), {"sweeps": []})
        }
        alpha = rows[str(SWEEP_A)]
        assert alpha["name"] == "alpha"
        assert alpha["state"] == "running"
        assert alpha["submitted_jobs"] == 2
        assert alpha["expected_trials"] == 8
        assert alpha["backend"] == "slurm"
        assert alpha["health"] == "failing"
        assert alpha["curation"] == ""
        beta = rows[str(SWEEP_B)]
        assert beta["backend"] == "local"
        assert beta["health"] == "healthy"

    def test_workspace_state_reapplies_sort_and_filters(self, service):
        state = {
            "quick": "alpha",
            "filters": {
                "state": {"filterType": "text", "type": "equals", "filter": "running"}
            },
            "sort": [{"colId": "backend", "sort": "desc"}],
        }
        page, _polls = page_content(
            "/dashboard/project/ops", service, workspace_state_doc={"ops": state}
        )
        grid = _grid(page, "sweep-grid")
        columns = {column["field"]: column for column in grid.columnDefs}
        assert columns["backend"]["sort"] == "desc"
        assert "sort" not in columns["name"]
        quick = next(
            node
            for node in _walk(page)
            if getattr(node, "id", None) == "workspace-quick"
        )
        assert quick.value == "alpha"


class TestCellTextSelection:
    """jernerics-eqn: AG Grid defaults to user-select: none, which makes
    identifier cells un-copyable. Every grid carries the documented pair
    through the shared helper and keeps its own dashGridOptions keys."""

    def test_grid_options_helper_is_the_documented_pair(self):
        assert grid_options() == {
            "enableCellTextSelection": True,
            "ensureDomOrder": True,
            "pagination": False,
        }
        assert grid_options(quickFilterText="x") == {
            "enableCellTextSelection": True,
            "ensureDomOrder": True,
            "pagination": False,
            "quickFilterText": "x",
        }

    def test_browser_and_family_grids_stay_selectable(self, service):
        workspace_page, _ = page_content("/dashboard/project/ops", service)
        sweep_options = _grid(workspace_page, "sweep-grid").dashGridOptions
        assert sweep_options["enableCellTextSelection"] is True
        assert sweep_options["ensureDomOrder"] is True
        assert sweep_options["rowSelection"] == {"mode": "multiRow"}

        inspector = _inspector(service, "sweep", SWEEP_A)
        family_options = _grid(inspector, {"focus-family": "grid"}).dashGridOptions
        assert family_options["enableCellTextSelection"] is True
        assert family_options["ensureDomOrder"] is True


class TestSweepInspector:
    def test_job_correlation_rows(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        jobs = {(job["scheduler_job_id"], job["role"]) for job in detail.jobs}
        assert jobs == {("9400001", "trials"), ("9400002", "checker")}
        assert {job["backend"] for job in detail.jobs} == {"slurm"}
        rendered = str(_inspector(service, "sweep", SWEEP_A))
        assert "9400001" in rendered
        assert "checker" in rendered

    def test_monitoring_counts_match_seeded_facts(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        overview = detail.overview
        assert overview.active == 1
        assert overview.quiet == 1
        assert overview.stale == 1
        assert overview.failed == 1
        assert overview.succeeded == 1
        assert overview.unknown == 1
        rendered = str(_inspector(service, "sweep", SWEEP_A))
        for label in ("active", "quiet", "stale", "failed", "succeeded"):
            assert f"{label} 1" in rendered
        assert "unknown 1" in rendered

    def test_monitoring_row_hides_zero_labels_and_notes_all_quiet(self, service):
        detail = service.sweep_detail(str(SWEEP_B))
        assert detail is not None
        row = _monitoring_counts(detail.overview)
        assert row.children is not None
        assert [badge.children for badge in row.children] == ["succeeded 1"]
        zeros = {label: 0 for label in _MONITORING_ORDER}
        quiet = _monitoring_badges(zeros)
        assert len(quiet) == 1
        assert quiet[0].children == "quiet"
        assert str(getattr(quiet[0], "className", "")) == "quiet-note"

    def test_progress_list_shows_in_flight_with_current_total_unit(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        progress = {row["execution_id"]: row for row in detail.progress}
        assert progress[str(E4)]["current"] == 7
        assert progress[str(E4)]["total"] == 10
        assert progress[str(E4)]["unit"] == "epoch"
        assert str(E5) in progress
        terminal_detail = service.sweep_detail(str(SWEEP_B))
        assert terminal_detail is not None
        assert terminal_detail.progress == []
        rendered = str(_inspector(service, "sweep", SWEEP_A))
        assert "7/10 epoch" in rendered
        assert "3/10 epoch" in rendered

        # jernerics-nqs: no empty-state boilerplate for terminal sweeps.
        assert "No in-flight executions report progress." not in str(
            _inspector(service, "sweep", SWEEP_B)
        )

    def test_executions_section_focuses_every_execution(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        assert {str(record.execution_id) for record in detail.executions} == {
            str(execution_id) for execution_id in (E1, E3, E4, E5, E6, E7)
        }
        grid = _grid(
            _inspector(service, "sweep", SWEEP_A), {"focus-executions": "grid"}
        )
        assert grid.id == {"focus-executions": "grid"}
        assert grid.getRowId == "params.data.execution_id"
        rows = {row["execution_id"]: row for row in grid.rowData}
        assert set(rows) == {
            str(execution_id) for execution_id in (E1, E3, E4, E5, E6, E7)
        }
        assert "node07" not in {row["host"] for row in rows.values()}
        finished = service.sweep_detail(str(SWEEP_B))
        assert finished is not None
        assert {str(record.execution_id) for record in finished.executions} == {str(E8)}
        finished_grid = _grid(
            _inspector(service, "sweep", SWEEP_B), {"focus-executions": "grid"}
        )
        assert {row["execution_id"] for row in finished_grid.rowData} == {str(E8)}
        assert "node07" in {row["host"] for row in finished_grid.rowData}

    def test_executions_table_shortens_hosts_and_keeps_times_single_line(self):
        ended = datetime.now(UTC) - timedelta(seconds=30)
        started = ended - timedelta(minutes=3)
        record = ExecutionRecord(
            execution_id=uuid.uuid4(),
            trial_id=uuid.uuid4(),
            hostname="node05.hpc.cluster.example.com",
            started_at=started,
            ended_at=ended,
            monitoring="active",
        )
        rendered = str(_executions_table([record], datetime_to_ns(ended)))
        assert "node05" in rendered  # first DNS label only
        assert "hpc.cluster.example.com" not in rendered
        assert "3m ago" in rendered  # relative-only cell text
        started_ns = datetime_to_ns(started)
        assert f"title='{absolute_time(started_ns)}'" in rendered
        assert rendered.count(absolute_time(started_ns)) == 1  # tooltip only

    def test_sweep_inspector_offers_close_control(self, service):
        rendered = str(_inspector(service, "sweep", SWEEP_A))
        assert "id='inspector-close'" in rendered
        assert f"Sweep alpha · {short_id(str(SWEEP_A))}" in rendered


class TestTrialFamilies:
    def test_one_row_per_root_with_current_generation(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        by_root = {row.root: row for row in detail.families}
        assert len(by_root) == 6
        family = by_root[str(F0)]
        assert family.current_trial == str(F2)
        assert family.state == "completed"
        assert family.objective == pytest.approx(0.75)
        assert family.retry_count == 2
        row = family_grid_row(family)
        assert row["params"] == "batch=32, depth=4, lr=0.1, +1"

    def test_family_rows_identify_root_and_current_trial(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        for family in detail.families:
            row = family_grid_row(family)
            assert row["root"] == family.root
            assert row["current_trial"] == family.current_trial
            assert row["root_short"] == short_id(family.root)
            assert row["current_short"] == short_id(family.current_trial)
        grid = _grid(_inspector(service, "sweep", SWEEP_A), {"focus-family": "grid"})
        columns = {column["field"]: column for column in grid.columnDefs}
        assert columns["root_short"]["field"] == "root_short"
        assert grid.getRowId == "params.data.root || params.data.trial_id"

    def test_lineage_side_panel_chain_is_exact(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        panel = lineage_panel([{"root": str(F0)}], {"lineage": detail.lineage})
        rendered = str(panel)
        chain = "cc110000 → cc120000 → cc130000"
        assert chain in rendered
        for index, trial, parent in (
            (0, "cc110000", "—"),
            (1, "cc120000", "cc110000"),
            (2, "cc130000", "cc120000"),
        ):
            assert f"Td({index})" in rendered
            assert f"Td('{trial}')" in rendered
            assert f"Td('{parent}')" in rendered

    def test_tray_from_grid_keeps_analysis_picks_and_flags(self):
        scope = tray_from_grid(
            [{"sweep_id": str(SWEEP_B)}, {"sweep_id": str(SWEEP_A)}],
            {
                "sweeps": [],
                "trials": [str(T4)],
                "families": [str(F0)],
                "executions": [],
                "expand": True,
                "include_archived": True,
                "include_invalid": False,
            },
        )
        assert scope == {
            "sweeps": [str(SWEEP_A), str(SWEEP_B)],
            "trials": [str(T4)],
            "families": [str(F0)],
            "executions": [],
            "expand": True,
            "include_archived": True,
            "include_invalid": False,
        }
        assert tray_from_grid([{"sweep_id": str(SWEEP_A)}], None)["sweeps"] == [
            str(SWEEP_A)
        ]


class TestTrialInspector:
    def test_family_header_params_catalog_and_executions(self, service):
        rendered = str(_inspector(service, "trial", F2))
        assert "cc110000 → cc120000 → cc130000" in rendered
        assert "retry index 2" in rendered
        assert "completed" in rendered
        assert "objective 0.75" in rendered
        assert "sampled" in rendered and "manual" in rendered
        assert "loss" in rendered
        assert "node01" in rendered and "node02" in rendered
        assert _focus_ref("execution", E1) in rendered
        assert _focus_ref("execution", E3) in rendered
        assert _focus_ref("sweep", SWEEP_A) in rendered
        assert "id='section-optimizer-state'" in rendered


class TestExecutionInspector:
    def test_timeline_failure_summary_and_separate_sections(self, service):
        rendered = str(_inspector(service, "execution", E1))
        assert "id='section-execution-facts'" in rendered
        assert "id='section-optimizer-state'" in rendered
        facts_at = rendered.index("id='section-execution-facts'")
        optimizer_at = rendered.index("id='section-optimizer-state'")
        assert facts_at != optimizer_at
        assert "boom: divide by zero" in rendered
        assert "exception" in rendered
        assert "Started" in rendered
        assert "Last heartbeat" in rendered
        assert "Last observation" in rendered
        assert "Ended" in rendered
        assert "unknown" in rendered
        assert "failed" in rendered  # optimizer trial state of F0

    def test_progress_params_resolved_config_and_provenance(self, service):
        rendered = str(_inspector(service, "execution", E4))
        assert "7/10 epoch" in rendered
        assert "host node03" in rendered
        assert '"lr": 0.1' in rendered
        assert "deadbeef" in rendered
        assert "sweep.yaml" in rendered
        assert "slurm" in rendered
        assert "section-optimizer-state" in rendered
        assert "running" in rendered

    def test_stale_execution_renders_stale_not_failed(self, service):
        rendered = str(_inspector(service, "execution", E6))
        assert "badge-stale" in rendered
        assert "Span(children='stale'" in rendered
        assert "failed" not in rendered

    def test_missing_heartbeat_renders_unknown(self, service):
        rendered = str(_inspector(service, "execution", E7))
        assert "badge-unknown" in rendered
        assert "Span(children='unknown'" in rendered
        assert "Last heartbeat" in rendered

    def test_execution_inspector_links_back_to_trial_and_sweep(self, service):
        detail = service.execution_detail(str(E4))
        assert detail is not None
        rendered = str(_inspector(service, "execution", E4))
        assert _focus_ref("trial", detail.context["trial_id"]) in rendered
        assert _focus_ref("sweep", SWEEP_A) in rendered
        assert "id='section-execution-artifacts'" in rendered


class TestPolling:
    def test_workspace_polls_while_any_sweep_is_incomplete(self, service):

        assert page_content("/dashboard/project/ops", service)[1] is True

    def test_focus_polls_only_while_the_focused_object_is_open(self, service):
        from jernerics_server.dashboard import workspace

        assert (
            workspace.focus_incomplete(service, {"kind": "sweep", "id": str(SWEEP_A)})
            is True
        )
        assert (
            workspace.focus_incomplete(service, {"kind": "sweep", "id": str(SWEEP_B)})
            is False
        )
        assert (
            workspace.focus_incomplete(service, {"kind": "trial", "id": str(T4)})
            is True
        )
        assert (
            workspace.focus_incomplete(service, {"kind": "trial", "id": str(F2)})
            is False
        )
        assert (
            workspace.focus_incomplete(service, {"kind": "execution", "id": str(E4)})
            is True
        )
        assert (
            workspace.focus_incomplete(service, {"kind": "execution", "id": str(E8)})
            is False
        )
        assert workspace.focus_incomplete(service, None) is False


class TestCurrentSemantics:
    def test_uncurated_sweeps_are_current(self, store_and_service):
        _, service = store_and_service
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        assert summaries["alpha"].current is True
        assert summaries["beta"].current is True

    def test_terminal_archived_sweep_is_not_current(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        assert summaries["beta"].archived is True
        assert summaries["beta"].current is False
        assert summaries["alpha"].current is True

    def test_incomplete_sweep_stays_current_despite_curation(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_A))
        store.mark_sweep_invalid(str(SWEEP_A), "misconfigured but still running")
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        alpha = summaries["alpha"]
        assert alpha.archived is True and alpha.invalid is True
        assert alpha.incomplete is True
        assert alpha.current is True

    def test_invalid_terminal_sweep_needs_both_restores(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        assert summaries["beta"].current is False
        store.restore_sweep_validity(str(SWEEP_B))
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        assert summaries["beta"].invalid is False
        assert summaries["beta"].archived is True
        assert summaries["beta"].current is False
        store.restore_sweep(str(SWEEP_B))
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        assert summaries["beta"].current is True


class TestProjectCatalogCuration:
    def _row(self, service, project):
        return {row.project: row for row in service.project_catalog()}[project]

    def test_terminal_archived_sweep_leaves_current_reads(self, curated):
        store, service = curated
        before = self._row(service, "curate")
        assert before.recent_sweep == "newer"
        assert before.succeeded == 2
        assert (before.archived_sweeps, before.invalid_sweeps) == (0, 0)
        store.archive_sweep(str(CUR_SWEEP_NEW))
        after = self._row(service, "curate")
        assert after.succeeded == 1
        assert after.recent_sweep == "older"
        assert after.last_activity_ns < before.last_activity_ns
        assert (after.archived_sweeps, after.invalid_sweeps) == (1, 0)

    def test_restored_sweep_returns_to_current_reads(self, curated):
        store, service = curated
        store.archive_sweep(str(CUR_SWEEP_NEW))
        store.restore_sweep(str(CUR_SWEEP_NEW))
        row = self._row(service, "curate")
        assert row.recent_sweep == "newer"
        assert row.succeeded == 2
        assert (row.archived_sweeps, row.invalid_sweeps) == (0, 0)

    def test_invalid_terminal_sweep_hidden_and_counted(self, curated):
        store, service = curated
        store.mark_sweep_invalid(str(CUR_SWEEP_NEW), "contaminated inputs")
        row = self._row(service, "curate")
        assert row.succeeded == 1
        assert row.recent_sweep == "older"
        assert row.last_activity_ns < self._row(service, "done").last_activity_ns
        assert (row.archived_sweeps, row.invalid_sweeps) == (1, 1)

    def test_fully_archived_project_stays_listed(self, curated):
        store, service = curated
        store.archive_sweep(str(CUR_SWEEP_DONE))
        rows = {row.project: row for row in service.project_catalog()}
        assert set(rows) == {"curate", "done"}
        done = rows["done"]
        assert (done.archived_sweeps, done.invalid_sweeps) == (1, 0)
        assert done.recent_sweep is None
        assert done.last_activity_ns is None
        assert (done.active, done.quiet, done.stale, done.succeeded) == (0, 0, 0, 0)

    def test_catalog_excludes_terminal_archived_sweep_from_ops_counts(
        self, store_and_service
    ):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        row = self._row(service, "ops")
        assert row.succeeded == 1
        assert (row.active, row.stale, row.failed) == (1, 1, 1)
        assert row.recent_sweep == "alpha"
        assert (row.archived_sweeps, row.invalid_sweeps) == (1, 0)


class TestOverviewCuration:
    def test_overview_returns_all_sweeps_with_curation_facts(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        assert set(summaries) == {"alpha", "beta"}
        beta = summaries["beta"]
        assert beta.archived is True
        assert beta.invalid is True
        assert beta.invalid_reason == "contaminated dataset"
        assert beta.archived_ns is not None and beta.invalid_ns is not None
        alpha = summaries["alpha"]
        assert alpha.archived_ns is None
        assert alpha.invalid_ns is None
        assert alpha.invalid_reason is None

    def test_explicit_sweep_read_returns_curated_sweep(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        store.mark_sweep_invalid(str(SWEEP_A), "kept for audit")
        beta = service.sweep_detail(str(SWEEP_B))
        assert beta is not None
        assert beta.overview.archived is True
        assert beta.overview.current is False
        alpha = service.sweep_detail(str(SWEEP_A))
        assert alpha is not None
        assert alpha.overview.invalid_reason == "kept for audit"
        assert alpha.overview.current is True


class TestOverviewTab:
    """jernerics-7bd: the overview is a bounded roll-up plus one
    virtualized grid row per sweep — never a card per sweep."""

    def test_no_project_and_empty_scope_guards(self, service):
        rendered = str(overview_tab(service, None, None))
        assert "Pick a project in the header" in rendered
        assert "overview-sweep-grid" not in rendered
        rendered = str(overview_tab(service, "ghost", None))
        assert "No sweeps tracked for project ghost yet." in rendered
        rendered = str(overview_tab(service, "ops", {"sweeps": [str(uuid.uuid4())]}))
        assert "No picked sweeps remain in project ops." in rendered

    def test_overview_never_fetches_per_sweep_detail(self, service, monkeypatch):
        def forbidden(_self, _sweep_id):
            raise AssertionError("overview render must not call sweep_detail")

        monkeypatch.setattr(DashboardService, "sweep_detail", forbidden)
        overview = overview_tab(service, "ops", {"sweeps": []})
        assert "overview-sweep-grid" in str(overview)

    def test_overview_is_one_rollup_section_plus_one_grid_section(self, service):
        overview = overview_tab(service, "ops", {"sweeps": []})
        sections = [
            node.className for node in _walk(overview) if isinstance(node, html.Section)
        ]
        assert sections == ["section overview-rollup", "section overview-sweeps"]
        rendered = str(overview)
        assert "Scope roll-up" in rendered
        assert "Sweeps in scope" in rendered

    def test_rollup_aggregates_scope_not_sweeps(self, service):
        rendered = str(overview_tab(service, "ops", {"sweeps": []}))
        assert "sweeps 2" in rendered
        assert "running 1" in rendered
        assert "completed 1" in rendered
        assert "health failing 1" in rendered
        assert "health healthy 1" in rendered
        assert "active 1" in rendered
        assert "succeeded 2" in rendered
        assert "in-flight executions 4" in rendered
        assert "last activity " in rendered
        assert "ago" in rendered

    def test_rollup_hides_zero_monitoring_and_notes_all_quiet(self, service):
        beta = next(
            row for row in service.sweep_overview("ops") if row.sweep_id == str(SWEEP_B)
        )
        rendered = str(overview_rollup([beta], 0))
        assert "succeeded 1" in rendered
        for label in ("active", "quiet", "stale", "failed", "unknown"):
            assert f"badge-{label}" not in rendered
        silent = replace(
            beta, active=0, quiet=0, stale=0, failed=0, succeeded=0, unknown=0
        )
        rendered = str(overview_rollup([silent], 0))
        assert "quiet" in rendered  # the single all-zero note
        assert "badge-active" not in rendered
        assert "succeeded" not in rendered

    def test_grid_rows_carry_operational_facts_and_stable_ids(self, service):
        overview = overview_tab(service, "ops", {"sweeps": []})
        grid = _grid(overview, "overview-sweep-grid")
        assert grid.getRowId == "params.data.sweep_id"
        options = grid.dashGridOptions
        assert options["enableCellTextSelection"] is True
        assert options["ensureDomOrder"] is True
        assert {column["field"] for column in grid.columnDefs} == {
            "name",
            "state",
            "health",
            "monitoring",
            "curation",
            "expected_trials",
            "last_activity",
        }
        rows = {row["sweep_id"]: row for row in grid.rowData}
        alpha = rows[str(SWEEP_A)]
        assert alpha["name"] == "alpha"
        assert alpha["state"] == "running"
        assert alpha["health"] == "failing"
        assert alpha["curation"] == ""
        assert alpha["expected_trials"] == 8
        assert alpha["monitoring"] == (
            "active 1 · quiet 1 · stale 1 · failed 1 · succeeded 1 · unknown 1"
        )
        assert alpha["last_activity"].endswith("ago")
        beta = rows[str(SWEEP_B)]
        assert beta["state"] == "completed"
        assert beta["expected_trials"] == 1
        assert beta["monitoring"] == "succeeded 1"

    def test_monitoring_column_clamps_with_full_value_reachable(self, service):
        """jernerics-l8f: the monitoring cell shares the clamped-cell
        policy; the full summary stays in rowData for title/popover."""
        overview = overview_tab(service, "ops", {"sweeps": []})
        grid = _grid(overview, "overview-sweep-grid")
        monitoring = next(
            column for column in grid.columnDefs if column["field"] == "monitoring"
        )
        assert monitoring["cellRenderer"] == "ClampedCell"
        assert monitoring["clampLimit"] == TEXT_LIMIT
        assert monitoring["maxWidth"] == 320
        rows = {row["sweep_id"]: row for row in grid.rowData}
        assert rows[str(SWEEP_A)]["monitoring"] == (
            "active 1 · quiet 1 · stale 1 · failed 1 · succeeded 1 · unknown 1"
        )

    def test_tray_scope_narrows_rollup_rows_and_keeps_curation(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        overview = overview_tab(service, "ops", {"sweeps": [str(SWEEP_B)]})
        grid = _grid(overview, "overview-sweep-grid")
        assert [row["sweep_id"] for row in grid.rowData] == [str(SWEEP_B)]
        assert grid.rowData[0]["curation"] == "invalid"
        rendered = str(overview)
        assert "sweeps 1" in rendered
        assert "completed 1" in rendered


class TestOverviewCurationVisibility:
    """jernerics-mqw: the overview region honors the Browse curation
    semantics — curated terminal sweeps leave roll-up and grid until
    included, while incomplete and picked sweeps never drop."""

    def test_archived_terminal_sweep_leaves_rollup_and_grid(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        overview = overview_tab(service, "ops", {"sweeps": []})
        grid = _grid(overview, "overview-sweep-grid")
        assert [row["sweep_id"] for row in grid.rowData] == [str(SWEEP_A)]
        rendered = str(overview)
        assert "sweeps 1" in rendered
        assert "completed" not in rendered

    def test_include_archived_brings_the_sweep_back(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        overview = overview_tab(
            service, "ops", {"sweeps": [], "include_archived": True}
        )
        grid = _grid(overview, "overview-sweep-grid")
        assert {row["sweep_id"] for row in grid.rowData} == {
            str(SWEEP_A),
            str(SWEEP_B),
        }
        assert "sweeps 2" in str(overview)

    def test_invalid_sweep_needs_its_own_include(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        overview = overview_tab(service, "ops", {"sweeps": []})
        grid = _grid(overview, "overview-sweep-grid")
        assert [row["sweep_id"] for row in grid.rowData] == [str(SWEEP_A)]
        overview = overview_tab(service, "ops", {"sweeps": [], "include_invalid": True})
        grid = _grid(overview, "overview-sweep-grid")
        assert {row["sweep_id"] for row in grid.rowData} == {
            str(SWEEP_A),
            str(SWEEP_B),
        }

    def test_incomplete_curated_sweep_stays_visible(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_A))
        store.mark_sweep_invalid(str(SWEEP_A), "still running")
        overview = overview_tab(service, "ops", {"sweeps": []})
        grid = _grid(overview, "overview-sweep-grid")
        assert [row["sweep_id"] for row in grid.rowData] == [
            str(SWEEP_A),
            str(SWEEP_B),
        ]
        assert grid.rowData[0]["curation"] == "invalid"

    def test_picked_curated_sweep_never_disappears(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        overview = overview_tab(service, "ops", {"sweeps": [str(SWEEP_B)]})
        grid = _grid(overview, "overview-sweep-grid")
        assert [row["sweep_id"] for row in grid.rowData] == [str(SWEEP_B)]
        assert "sweeps 1" in str(overview)

    def test_fully_curated_project_names_the_curation(self, curated):
        store, service = curated
        store.archive_sweep(str(CUR_SWEEP_OLD))
        store.archive_sweep(str(CUR_SWEEP_NEW))
        rendered = str(overview_tab(service, "curate", {"sweeps": []}))
        assert "No current sweeps in project curate" in rendered


class TestNoSqlInCallbacks:
    _FORBIDDEN_SQL = re.compile(r"SELECT")
    _FORBIDDEN_MODULES = re.compile(r"sqlite|httpx")

    def test_dashboard_modules_contain_no_sql_or_direct_clients(self):
        root = Path(__file__).parent.parent / "src" / "jernerics_server" / "dashboard"
        for path in sorted(root.glob("*.py")):
            source = path.read_text()
            assert not self._FORBIDDEN_SQL.search(source), path
            assert not self._FORBIDDEN_MODULES.search(source), path


class TestRoutesServe:
    def test_every_route_returns_200_with_session_cookie(self, authed):
        for url in (
            "/dashboard/",
            "/dashboard/project/ops",
        ):
            response = authed.get(url)
            assert response.status_code == 200, url
            assert "react-entry-point" in response.text


class TestServiceCurationMutations:
    def test_archive_and_restore_report_labels_and_change_reads(self, mutable):
        _store, service = mutable
        assert service.archive_sweep(str(SWEEP_B)) == "beta"
        beta = {row.name: row for row in service.sweep_overview("ops")}["beta"]
        assert beta.archived and not beta.invalid
        assert service.archive_sweep(str(SWEEP_B)) == "beta"  # retry is a no-op
        assert service.restore_sweep(str(SWEEP_B)) == "beta"
        beta = {row.name: row for row in service.sweep_overview("ops")}["beta"]
        assert beta.current is True

    def test_mark_invalid_persists_reason_and_archives(self, mutable):
        _store, service = mutable
        assert service.mark_sweep_invalid(str(SWEEP_B), "  contaminated dataset ") == (
            "beta"
        )
        beta = {row.name: row for row in service.sweep_overview("ops")}["beta"]
        assert beta.invalid is True
        assert beta.invalid_reason == "contaminated dataset"
        assert beta.archived is True  # invalid implies an archived fact
        assert beta.archived_ns is not None and beta.invalid_ns is not None

    def test_blank_or_overlong_reason_is_rejected_by_the_store(self, mutable):
        _store, service = mutable
        for reason in ("   ", "", "x" * 501):
            with pytest.raises(
                CurationRejectedError, match=r"1\.\.500 characters after trimming"
            ):
                service.mark_sweep_invalid(str(SWEEP_B), reason)

    def test_restore_validity_leaves_the_archived_fact(self, mutable):
        _store, service = mutable
        service.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        assert service.restore_sweep_validity(str(SWEEP_B)) == "beta"
        beta = {row.name: row for row in service.sweep_overview("ops")}["beta"]
        assert beta.invalid is False and beta.invalid_reason is None
        assert beta.archived is True and beta.current is False

    def test_restore_while_invalid_names_the_transition(self, mutable):
        _store, service = mutable
        service.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        with pytest.raises(CurationRejectedError, match="restore validity"):
            service.restore_sweep(str(SWEEP_B))

    def test_unknown_sweep_id_is_rejected_visibly(self, mutable):
        _store, service = mutable
        with pytest.raises(CurationRejectedError, match="no sweep matches"):
            service.archive_sweep(str(uuid.uuid4()))

    def test_mutations_without_a_store_raise_a_clear_error(self, service):
        with pytest.raises(CurationUnavailableError, match="no write store"):
            service.archive_sweep(str(SWEEP_B))


class TestBrowserDiscovery:
    def test_terminal_archived_hidden_until_included(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        summaries = service.sweep_overview("ops")
        rows = browser_sweep_rows(summaries, {"sweeps": []})
        assert [row["sweep_id"] for row in rows] == [str(SWEEP_A)]
        rows = browser_sweep_rows(summaries, {"sweeps": []}, include_archived=True)
        assert {row["sweep_id"] for row in rows} == {str(SWEEP_A), str(SWEEP_B)}
        rows = browser_sweep_rows(summaries, {"sweeps": []}, include_invalid=True)
        assert [row["sweep_id"] for row in rows] == [str(SWEEP_A)]

    def test_terminal_invalid_needs_its_own_include(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        summaries = service.sweep_overview("ops")
        rows = browser_sweep_rows(summaries, {"sweeps": []})
        assert [row["sweep_id"] for row in rows] == [str(SWEEP_A)]
        rows = browser_sweep_rows(summaries, {"sweeps": []}, include_archived=True)
        assert [row["sweep_id"] for row in rows] == [str(SWEEP_A)]
        rows = browser_sweep_rows(summaries, {"sweeps": []}, include_invalid=True)
        assert {row["sweep_id"] for row in rows} == {str(SWEEP_A), str(SWEEP_B)}

    def test_picked_curated_sweep_is_never_dropped(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "kept for audit")
        summaries = service.sweep_overview("ops")
        rows = browser_sweep_rows(summaries, {"sweeps": [str(SWEEP_B)]})
        beta = next(row for row in rows if row["sweep_id"] == str(SWEEP_B))
        assert beta["curation"] == "invalid"

    def test_incomplete_curated_sweep_stays_discoverable_with_note(
        self, store_and_service
    ):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_A))
        store.mark_sweep_invalid(str(SWEEP_A), "still running")
        rows = browser_sweep_rows(service.sweep_overview("ops"), {"sweeps": []})
        assert [row["sweep_id"] for row in rows] == [str(SWEEP_A), str(SWEEP_B)]
        note = str(curation_note(rows))
        assert "alpha is invalid" in note
        assert "does not cancel or hide active work" in note
        assert rows[0]["incomplete"] is True and rows[0]["invalid"] is True

    def test_picked_terminal_curated_note_states_why_it_is_listed(
        self, store_and_service
    ):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        rows = browser_sweep_rows(
            service.sweep_overview("ops"), {"sweeps": [str(SWEEP_B)]}
        )
        assert [row["sweep_id"] for row in rows] == [str(SWEEP_A), str(SWEEP_B)]
        note = str(curation_note(rows))
        assert "picked or included" in note
        assert "alpha" not in note

    def test_grid_rows_carry_distinct_curation_markers(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        store.mark_sweep_invalid(str(SWEEP_A), "misconfigured")
        rows = browser_sweep_rows(
            service.sweep_overview("ops"),
            {"sweeps": [str(SWEEP_A), str(SWEEP_B)]},
        )
        by_id = {row["sweep_id"]: row for row in rows}
        assert by_id[str(SWEEP_A)]["curation"] == "invalid"
        assert by_id[str(SWEEP_B)]["curation"] == "archived"


class TestWorkspaceActions:
    def test_curation_transitions_matrix(self):
        assert curation_transitions(False, False) == {
            "archive": True,
            "invalid": True,
            "restore_validity": False,
            "restore": False,
        }
        assert curation_transitions(True, False) == {
            "archive": False,
            "invalid": True,
            "restore_validity": False,
            "restore": True,
        }
        assert curation_transitions(True, True) == {
            "archive": False,
            "invalid": False,
            "restore_validity": True,
            "restore": False,
        }

    def test_selection_gates_from_real_grid_rows(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_A), "contaminated")
        rows = browser_sweep_rows(service.sweep_overview("ops"), {"sweeps": []})
        by_id = {row["sweep_id"]: row for row in rows}
        none = {
            "archive": False,
            "invalid": False,
            "restore_validity": False,
            "restore": False,
        }
        assert selection_transitions([]) == none
        assert selection_transitions([by_id[str(SWEEP_A)]]) == {
            "archive": False,
            "invalid": False,
            "restore_validity": True,
            "restore": False,
        }
        assert selection_transitions([by_id[str(SWEEP_B)]]) == {
            "archive": True,
            "invalid": True,
            "restore_validity": False,
            "restore": False,
        }
        offered = selection_transitions([by_id[str(SWEEP_A)], by_id[str(SWEEP_B)]])
        assert offered == {
            "archive": True,
            "invalid": True,
            "restore_validity": True,
            "restore": False,
        }


class TestWorkspacePersistence:
    def test_sort_extraction_from_column_state(self):
        assert sort_from_columns(
            [
                {"colId": "name", "sort": None, "width": 120},
                {"colId": "backend", "sort": "desc", "width": 90},
                "junk",
            ]
        ) == [{"colId": "backend", "sort": "desc"}]
        assert sort_from_columns([]) is None
        assert sort_from_columns(None) is None

    def test_defaults_and_saved_state_per_project(self):
        assert workspace_state(None, "ops") == {
            "quick": "",
            "filters": None,
            "sort": None,
        }
        saved = {"ops": {"quick": "x"}}
        assert workspace_state(saved, "ops")["quick"] == "x"
        assert workspace_state(saved, "beta")["quick"] == ""

    def test_remember_merges_only_the_edited_field(self):
        current = {
            "ops": {
                "quick": "x",
                "filters": None,
                "sort": [{"colId": "name", "sort": "asc"}],
            }
        }
        updated = remember_workspace(current, "ops", quick="")
        assert updated is not None
        assert updated["ops"] == {
            "quick": "",
            "filters": None,
            "sort": [{"colId": "name", "sort": "asc"}],
        }
        assert remember_workspace(updated, "ops", quick="") is None
        other = remember_workspace(updated, "beta", quick="y")
        assert other is not None
        assert other["ops"] == updated["ops"]
        assert other["beta"]["quick"] == "y"


class TestApplyCuration:
    def test_blank_reason_is_rejected_before_dispatch(self, mutable):
        store, service = mutable
        ok, report = apply_curation(service, "invalid", [str(SWEEP_B)], "   ")
        assert ok is False
        assert "requires a reason" in report
        assert store._curation_row(str(SWEEP_B))[1] is None

    def test_partial_failure_never_claims_all_succeeded(self, mutable):
        _store, service = mutable
        ghost = str(uuid.uuid4())
        ok, report = apply_curation(service, "archive", [str(SWEEP_B), ghost])
        assert ok is False
        assert "Archived beta" in report
        assert "Failed" in report and ghost.replace("-", "")[:8] in report

    def test_remarking_an_already_invalid_sweep_is_reported_not_rewritten(
        self, mutable
    ):
        store, service = mutable
        store.mark_sweep_invalid(str(SWEEP_B), "first reason")
        before = store._curation_row(str(SWEEP_B))
        ok, report = apply_curation(service, "invalid", [str(SWEEP_B)], "second try")
        assert ok is True
        assert "beta" in report and "already invalid" in report
        assert store._curation_row(str(SWEEP_B)) == before

    def test_mixed_invalid_selection_marks_only_the_fresh_sweep(self, mutable):
        store, service = mutable
        store.mark_sweep_invalid(str(SWEEP_B), "kept reason")
        ok, report = apply_curation(
            service, "invalid", [str(SWEEP_A), str(SWEEP_B)], "fresh reason"
        )
        assert ok is True
        assert "Marked invalid alpha" in report
        assert "already invalid" in report and "beta" in report
        assert store._curation_row(str(SWEEP_B))[2] == "kept reason"
        assert store._curation_row(str(SWEEP_A))[1] is not None


class TestSweepInspectorCuration:
    def test_archived_banner_and_disabled_transitions(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        rendered = str(_inspector(service, "sweep", SWEEP_B))
        assert "This sweep is archived" in rendered
        assert "id='detail-curation'" in rendered
        assert "id='detail-reason'" in rendered
        assert "id='detail-message'" in rendered
        assert "badge-archived" in rendered

    def test_invalid_banner_names_reason_and_timestamp(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        rendered = str(_inspector(service, "sweep", SWEEP_B))
        assert "Marked scientifically invalid" in rendered
        assert "contaminated dataset" in rendered
        assert "UTC" in rendered
        assert "badge-invalid" in rendered

    def test_inspector_buttons_follow_valid_transitions(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        buttons = {
            node.id: node
            for node in _walk(_inspector(service, "sweep", SWEEP_B))
            if isinstance(getattr(node, "id", None), str)
            and node.id.startswith("detail-")
        }
        assert buttons["detail-archive"].disabled is True
        assert buttons["detail-invalid"].disabled is True
        assert buttons["detail-restore-validity"].disabled is False
        assert buttons["detail-restore"].disabled is True

    def test_detail_curation_banners_and_buttons(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "kept for audit")
        detail = service.sweep_detail(str(SWEEP_B))
        assert detail is not None
        rendered = str(detail_curation(detail.overview))
        assert "badge-invalid" in rendered
        assert "kept for audit" in rendered
        assert "Mark this sweep invalid" in rendered
        assert ">Mark invalid<" not in rendered


class TestProjectPageCuration:
    def test_rows_show_separate_archived_and_invalid_counts(self, curated):
        store, service = curated
        store.archive_sweep(str(CUR_SWEEP_NEW))
        store.mark_sweep_invalid(str(CUR_SWEEP_DONE), "wrong dataset")
        page, _polls = page_content("/dashboard/", service)
        rendered = str(page)
        assert "Span(children='archived 1', className='badge badge-archived')" in (
            rendered
        )
        assert "Span(children='invalid 1', className='badge badge-invalid')" in (
            rendered
        )

    def test_failed_terminal_curated_work_keeps_current_health(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        page, _polls = page_content("/dashboard/", service)
        rendered = str(page)
        assert "failed 1" in rendered  # alpha's failed execution stays Current


class TestMountedCurationJourney:
    """The registered curation callbacks, driven through Dash's dispatch
    endpoint exactly as the browser would."""

    @staticmethod
    def _callback_key(callback_map, wanted: set[str]) -> str:
        def outputs_of(key):
            stripped = key.removeprefix("..").removesuffix("..")
            return {part.split("@")[0] for part in stripped.split("...") if part}

        return next(key for key in callback_map if outputs_of(key) == wanted)

    def _dispatch(self, client, callback_map, wanted, inputs, state=(), changed=None):
        key = self._callback_key(callback_map, wanted)
        specs = [
            part.split("@")[0]
            for part in key.removeprefix("..").removesuffix("..").split("...")
            if part
        ]
        outputs = [
            {"id": spec.split(".")[0], "property": spec.split(".")[1]} for spec in specs
        ]
        response = client.post(
            "/dashboard/_dash-update-component",
            json={
                "output": key,
                "outputs": outputs[0] if len(outputs) == 1 else outputs,
                "inputs": inputs,
                "state": list(state),
                "changedPropIds": changed
                or [f"{i['id']}.{i['property']}" for i in inputs],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["response"]

    def _callback_map(self, client):
        from jernerics_server.dashboard.app import build_dash_app

        app = client.app
        ctx = app.state.dashboard
        return build_dash_app(ctx).callback_map

    @staticmethod
    def _grid_row(store, sweep_id: uuid.UUID) -> dict:
        service = DashboardService(QueryService(store))
        return next(
            row
            for row in browser_sweep_rows(
                service.sweep_overview("ops"), {"sweeps": [str(sweep_id)]}
            )
            if row["sweep_id"] == str(sweep_id)
        )

    _WORKSPACE_OUTPUTS = {
        "workspace-message.children",
        "sweep-grid.rowData",
        "sweep-grid.selectedRows",
        "workspace-curation-note.children",
    }

    _TRANSITIONS_OUTPUTS = {
        "ws-archive.disabled",
        "ws-invalid.disabled",
        "ws-restore-validity.disabled",
        "ws-restore.disabled",
        "ws-reason.style",
        "ws-curation-summary.children",
    }

    def test_selection_gates_reason_and_counts_picked_rows(self, mutable_client):
        store, client = mutable_client
        callback_map = self._callback_map(client)

        def transitions(rows):
            return self._dispatch(
                client,
                callback_map,
                self._TRANSITIONS_OUTPUTS,
                [{"id": "sweep-grid", "property": "selectedRows", "value": rows}],
            )

        response = transitions([self._grid_row(store, SWEEP_B)])
        assert response["ws-archive"]["disabled"] is False
        assert response["ws-invalid"]["disabled"] is False
        assert response["ws-reason"]["style"] == {}  # reason reveals with the action
        assert response["ws-curation-summary"]["children"] == "Curation (1 picked)"

        store.mark_sweep_invalid(str(SWEEP_B), "bad science")
        curated = [self._grid_row(store, SWEEP_B)]
        response = transitions(curated)
        assert response["ws-archive"]["disabled"] is True
        assert response["ws-invalid"]["disabled"] is True
        assert response["ws-reason"]["style"] == {"display": "none"}
        assert response["ws-restore-validity"]["disabled"] is False

        response = transitions([])
        assert response["ws-curation-summary"]["children"] == "Curation…"
        assert response["ws-reason"]["style"] == {"display": "none"}

    def test_workspace_archive_refreshes_grid_and_clears_selection(
        self, mutable_client
    ):
        store, client = mutable_client
        callback_map = self._callback_map(client)
        row = self._grid_row(store, SWEEP_B)
        response = self._dispatch(
            client,
            callback_map,
            self._WORKSPACE_OUTPUTS,
            [
                {"id": "ws-archive", "property": "n_clicks", "value": 1},
                {"id": "ws-invalid", "property": "n_clicks", "value": 0},
                {"id": "ws-restore-validity", "property": "n_clicks", "value": 0},
                {"id": "ws-restore", "property": "n_clicks", "value": 0},
            ],
            state=[
                {"id": "sweep-grid", "property": "selectedRows", "value": [row]},
                {"id": "ws-reason", "property": "value", "value": ""},
                {"id": "project-store", "property": "data", "value": "ops"},
                {"id": "view-store", "property": "data", "value": None},
            ],
            changed=["ws-archive.n_clicks"],
        )
        assert response["workspace-message"]["children"]["props"][
            "children"
        ].startswith("Archived beta")
        row_ids = [entry["sweep_id"] for entry in response["sweep-grid"]["rowData"]]
        assert row_ids == [str(SWEEP_A)]  # beta left discovery until included
        assert response["sweep-grid"]["selectedRows"] == []
        assert store._curation_row(str(SWEEP_B))[0] is not None

    def test_workspace_mark_invalid_requires_reason(self, mutable_client):
        store, client = mutable_client
        callback_map = self._callback_map(client)
        row = self._grid_row(store, SWEEP_B)
        response = self._dispatch(
            client,
            callback_map,
            self._WORKSPACE_OUTPUTS,
            [
                {"id": "ws-archive", "property": "n_clicks", "value": 0},
                {"id": "ws-invalid", "property": "n_clicks", "value": 1},
                {"id": "ws-restore-validity", "property": "n_clicks", "value": 0},
                {"id": "ws-restore", "property": "n_clicks", "value": 0},
            ],
            state=[
                {"id": "sweep-grid", "property": "selectedRows", "value": [row]},
                {"id": "ws-reason", "property": "value", "value": "   "},
                {"id": "project-store", "property": "data", "value": "ops"},
                {"id": "view-store", "property": "data", "value": None},
            ],
            changed=["ws-invalid.n_clicks"],
        )
        message = response["workspace-message"]["children"]["props"]["children"]
        assert "requires a reason" in message
        assert store._curation_row(str(SWEEP_B))[1] is None  # nothing dispatched

    _DETAIL_OUTPUTS = {
        "detail-message.children",
        "detail-curation.children",
        "sweep-grid.rowData",
        "sweep-grid.selectedRows",
        "workspace-curation-note.children",
    }

    def test_detail_mark_invalid_persists_reason_and_banner(self, mutable_client):
        store, client = mutable_client
        callback_map = self._callback_map(client)
        response = self._dispatch(
            client,
            callback_map,
            self._DETAIL_OUTPUTS,
            [
                {"id": "detail-archive", "property": "n_clicks", "value": 0},
                {"id": "detail-invalid", "property": "n_clicks", "value": 1},
                {"id": "detail-restore-validity", "property": "n_clicks", "value": 0},
                {"id": "detail-restore", "property": "n_clicks", "value": 0},
            ],
            state=[
                {
                    "id": "view-store",
                    "property": "data",
                    "value": {
                        "focus": {"kind": "sweep", "id": str(SWEEP_B)},
                        "scope": {
                            "sweeps": [str(SWEEP_B)],
                            "trials": [],
                            "families": [],
                        },
                    },
                },
                {"id": "detail-reason", "property": "value", "value": "bad shards"},
                {"id": "sweep-grid", "property": "selectedRows", "value": None},
                {"id": "project-store", "property": "data", "value": "ops"},
            ],
            changed=["detail-invalid.n_clicks"],
        )
        assert response["detail-message"]["children"]["props"]["children"] == (
            "Marked invalid beta."
        )
        rendered = str(response["detail-curation"]["children"])
        assert "bad shards" in rendered
        row = store._curation_row(str(SWEEP_B))
        assert row[1] is not None and row[2] == "bad shards"
        row_ids = [entry["sweep_id"] for entry in response["sweep-grid"]["rowData"]]
        grid_row = next(
            entry
            for entry in response["sweep-grid"]["rowData"]
            if entry["sweep_id"] == str(SWEEP_B)
        )
        assert grid_row["invalid"] is True and grid_row["curation"] == "invalid"

    _TICK_OUTPUTS = {
        "sweep-grid.rowData",
        "sweep-grid.selectedRows",
        "workspace-curation-note.children",
        "sweep-browser-facts-store.data",
    }

    def test_tick_refreshes_grid_data_and_keeps_selection(self, mutable_client):
        _store, client = mutable_client
        callback_map = self._callback_map(client)
        doc = {"scope": {"sweeps": [str(SWEEP_B)], "trials": [], "families": []}}
        response = self._dispatch(
            client,
            callback_map,
            self._TICK_OUTPUTS,
            [
                {"id": "project-store", "property": "data", "value": "ops"},
                {"id": "view-store", "property": "data", "value": doc},
                {"id": "poll", "property": "n_intervals", "value": 3},
            ],
            state=[
                {"id": "sweep-grid", "property": "selectedRows", "value": None},
                {"id": "sweep-browser-facts-store", "property": "data", "value": None},
            ],
            changed=["poll.n_intervals"],
        )
        row_ids = [row["sweep_id"] for row in response["sweep-grid"]["rowData"]]
        assert row_ids == [str(SWEEP_A), str(SWEEP_B)]
        picked = [row["sweep_id"] for row in response["sweep-grid"]["selectedRows"]]
        assert picked == [str(SWEEP_B)]

    def test_workspace_mark_invalid_keeps_picked_row_and_selection(
        self, mutable_client
    ):
        """A picked terminal curated sweep must not vanish between the
        action and the next poll: fresh rows recompute from the CURRENT
        tray, and the selection keeps the surviving row."""
        store, client = mutable_client
        callback_map = self._callback_map(client)
        row = self._grid_row(store, SWEEP_B)
        doc = {"scope": {"sweeps": [str(SWEEP_B)], "trials": [], "families": []}}
        response = self._dispatch(
            client,
            callback_map,
            self._WORKSPACE_OUTPUTS,
            [
                {"id": "ws-archive", "property": "n_clicks", "value": 0},
                {"id": "ws-invalid", "property": "n_clicks", "value": 1},
                {"id": "ws-restore-validity", "property": "n_clicks", "value": 0},
                {"id": "ws-restore", "property": "n_clicks", "value": 0},
            ],
            state=[
                {"id": "sweep-grid", "property": "selectedRows", "value": [row]},
                {"id": "ws-reason", "property": "value", "value": "bad shards"},
                {"id": "project-store", "property": "data", "value": "ops"},
                {"id": "view-store", "property": "data", "value": doc},
            ],
            changed=["ws-invalid.n_clicks"],
        )
        row_ids = [entry["sweep_id"] for entry in response["sweep-grid"]["rowData"]]
        assert row_ids == [str(SWEEP_A), str(SWEEP_B)]
        kept = response["sweep-grid"]["selectedRows"]
        assert [entry["sweep_id"] for entry in kept] == [str(SWEEP_B)]
        assert kept[0]["invalid"] is True and kept[0]["archived"] is True
        message = response["workspace-message"]["children"]["props"]["children"]
        assert message.startswith("Marked invalid beta")
        assert store._curation_row(str(SWEEP_B))[1] is not None

    def test_selection_survives_tick_before_it_lands_in_the_tray(self, mutable_client):
        """A poll tick dispatching before the selection reaches the view
        doc must not clear the grid selection."""
        _store, client = mutable_client
        callback_map = self._callback_map(client)
        doc = {"scope": {"sweeps": [], "trials": [], "families": []}}
        response = self._dispatch(
            client,
            callback_map,
            self._TICK_OUTPUTS,
            [
                {"id": "project-store", "property": "data", "value": "ops"},
                {"id": "view-store", "property": "data", "value": doc},
                {"id": "poll", "property": "n_intervals", "value": 5},
            ],
            state=[
                {
                    "id": "sweep-grid",
                    "property": "selectedRows",
                    "value": [{"sweep_id": str(SWEEP_B)}],
                },
                {"id": "sweep-browser-facts-store", "property": "data", "value": None},
            ],
        )
        row_ids = [entry["sweep_id"] for entry in response["sweep-grid"]["rowData"]]
        assert row_ids == [str(SWEEP_A), str(SWEEP_B)]
        picked = [entry["sweep_id"] for entry in response["sweep-grid"]["selectedRows"]]
        assert picked == [str(SWEEP_B)]


class TestFailureView:
    """jernerics-gcj: the roll-up's failed badge opens a scope-wide
    failure view; kind and summary read inline, trials focus in one
    click, and marking the sweep invalid acts from that context.
    jernerics-zdpq: many parallel failures from one shared bug die in
    one batched mark-invalid — group checkboxes, a select-all, and a
    single apply_curation call."""

    _dispatch = TestMountedCurationJourney._dispatch
    _callback_key = staticmethod(TestMountedCurationJourney._callback_key)
    _callback_map = TestMountedCurationJourney._callback_map

    def test_service_lists_failed_executions_with_kind_and_summary(self, service):
        rows = service.failed_executions("ops")
        assert [(row.trial_number, row.failure_kind) for row in rows] == [
            (1, "exception")
        ]
        row = rows[0]
        assert row.sweep_id == str(SWEEP_A)
        assert row.sweep_name == "alpha"
        assert row.trial_id == str(F0)
        assert row.failure_summary == "boom: divide by zero"

    def test_hidden_curated_sweep_failures_stay_out(self, curated):
        store, service = curated
        now = datetime.now(UTC)
        failure = uuid.uuid4()
        result = IngestService(store).apply(
            IngestRequest(
                protocol_version=PROTOCOL_VERSION,
                events=[
                    ExecutionStartEvent(
                        event_id=uuid.uuid4(),
                        recorded_at=now,
                        execution_id=failure,
                        trial_id=CUR_T1,
                        hostname="node01",
                        started_at=now,
                    ),
                    ExecutionEndEvent(
                        event_id=uuid.uuid4(),
                        recorded_at=now,
                        execution_id=failure,
                        ended_at=now,
                        outcome=ExecutionOutcome.FAILURE,
                        exit_code=1,
                        failure_kind=FailureKind.TIMEOUT,
                        failure_summary="killed after 3600s",
                    ),
                ],
            )
        )
        assert not result.conflicts
        assert [row.failure_kind for row in service.failed_executions("curate")] == [
            "timeout"
        ]
        store.mark_sweep_invalid(str(CUR_SWEEP_OLD), "bad shard map")
        assert service.failed_executions("curate") == []

    def test_panel_groups_failures_with_focus_and_summary(self, service):
        scoped = scoped_sweeps(service.sweep_overview("ops"), None)
        rendered = str(failed_view_panel(service, "ops", scoped, 0))
        assert "boom: divide by zero" in rendered
        assert "exception" in rendered
        assert _focus_ref("trial", F0) in rendered
        assert _focus_ref("sweep", SWEEP_A) in rendered
        assert "Mark sweep invalid" in rendered

    def test_overview_embeds_failure_view_only_when_failing(self, service):
        overview = overview_tab(service, "ops", {"sweeps": []})
        rendered = str(overview)
        assert "failed-trials-view" in rendered
        assert "failed-view-open" in rendered
        assert "failed 1" in rendered
        beta = scoped_sweeps(service.sweep_overview("ops"), {"sweeps": [str(SWEEP_B)]})
        panel = failed_view_panel(service, "ops", beta, 0)
        assert "No failed executions in scope." in str(panel)

    def test_failure_view_absent_without_failures(self, curated):
        _store, service = curated
        rendered = str(overview_tab(service, "curate", {"sweeps": []}))
        assert "failed-trials-view" not in rendered

    _FAILED_OUTPUTS = {
        "failed-trials-panel.children",
        "failed-trials-view.open",
        "sweep-grid.rowData",
        "sweep-grid.selectedRows",
        "workspace-curation-note.children",
        '{"failed-sweep":["ALL"]}.value',
    }

    def _failed_state(self, reason: str) -> list[dict]:
        return [
            {"id": "failed-reason", "property": "value", "value": reason},
            {"id": "project-store", "property": "data", "value": "ops"},
            {"id": "view-store", "property": "data", "value": None},
            {"id": "sweep-grid", "property": "selectedRows", "value": None},
        ]

    def _failed_inputs(
        self,
        group_ids: list[str],
        *,
        batch: int = 0,
        checked: set[str] | None = None,
        select_all: list | None = None,
    ) -> list:
        picked = checked or set()
        return [
            {"id": "failed-view-open", "property": "n_clicks", "value": 1},
            [
                {"id": {"failed-invalid": sid}, "property": "n_clicks", "value": 0}
                for sid in group_ids
            ],
            {"id": "failed-invalid-batch", "property": "n_clicks", "value": batch},
            [
                {
                    "id": {"failed-sweep": sid},
                    "property": "value",
                    "value": [sid] if sid in picked else [],
                }
                for sid in group_ids
            ],
            {
                "id": "failed-select-all",
                "property": "value",
                "value": select_all or [],
            },
        ]

    def _failed_payload(
        self,
        callback_map,
        inputs,
        state,
        changed,
        group_ids: list[str],
    ) -> dict:
        """Dispatch body the way the browser sends it: the wildcard
        output expands to one concrete entry per mounted checklist."""
        key = self._callback_key(callback_map, self._FAILED_OUTPUTS)
        specs = [
            part.split("@")[0]
            for part in key.removeprefix("..").removesuffix("..").split("...")
            if part
        ]
        outputs = []
        for spec in specs:
            prop = spec.rsplit(".", 1)[1]
            if spec.startswith("{"):
                outputs.append(
                    [
                        {"id": {"failed-sweep": sid}, "property": prop}
                        for sid in group_ids
                    ]
                )
            else:
                outputs.append({"id": spec.rsplit(".", 1)[0], "property": prop})
        return {
            "output": key,
            "outputs": outputs,
            "inputs": inputs,
            "state": list(state),
            "changedPropIds": changed,
        }

    def _failed_dispatch(self, client, callback_map, inputs, state, changed, group_ids):
        response = client.post(
            "/dashboard/_dash-update-component",
            json=self._failed_payload(callback_map, inputs, state, changed, group_ids),
        )
        assert response.status_code == 200, response.text
        return response.json()["response"]

    def _fail_sweep_b(self, store) -> None:
        """A failed execution on beta's only trial (T9) so the ops scope
        carries two failed-sweep groups."""
        now = datetime.now(UTC)
        execution = uuid.uuid4()
        result = IngestService(store).apply(
            IngestRequest(
                protocol_version=PROTOCOL_VERSION,
                events=[
                    ExecutionStartEvent(
                        event_id=uuid.uuid4(),
                        recorded_at=now,
                        execution_id=execution,
                        trial_id=T9,
                        hostname="node09",
                        started_at=now,
                    ),
                    ExecutionEndEvent(
                        event_id=uuid.uuid4(),
                        recorded_at=now,
                        execution_id=execution,
                        ended_at=now,
                        outcome=ExecutionOutcome.FAILURE,
                        exit_code=1,
                        failure_kind=FailureKind.TIMEOUT,
                        failure_summary="killed after 3600s",
                    ),
                ],
            )
        )
        assert not result.conflicts

    def test_badge_opens_panel_with_kind_and_summary(self, mutable_client):
        _store, client = mutable_client
        callback_map = self._callback_map(client)
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs([str(SWEEP_A)]),
            self._failed_state(""),
            changed=["failed-view-open.n_clicks"],
            group_ids=[str(SWEEP_A)],
        )
        assert response["failed-trials-view"]["open"] is True
        children = str(response["failed-trials-panel"]["children"])
        assert "boom: divide by zero" in children
        assert "exception" in children

    def test_panel_renders_batch_controls_and_group_checkboxes(self, store_and_service):
        store, service = store_and_service
        self._fail_sweep_b(store)
        scoped = scoped_sweeps(service.sweep_overview("ops"), None)
        rendered = failed_view_panel(service, "ops", scoped, 0)
        check_ids = [
            node.id
            for node in _walk(html.Div(rendered))
            if isinstance(node, dcc.Checklist)
        ]
        assert {"failed-sweep": str(SWEEP_A)} in check_ids
        assert {"failed-sweep": str(SWEEP_B)} in check_ids
        assert "failed-select-all" in check_ids
        button_ids = [
            node.id
            for node in _walk(html.Div(rendered))
            if isinstance(node, html.Button)
        ]
        assert "failed-invalid-batch" in button_ids
        assert {"failed-invalid": str(SWEEP_A)} in button_ids

    def test_panel_omits_batch_controls_without_failures(self, service):
        beta = scoped_sweeps(service.sweep_overview("ops"), {"sweeps": [str(SWEEP_B)]})
        rendered = str(failed_view_panel(service, "ops", beta, 0))
        assert "No failed executions in scope." in rendered
        assert "failed-select-all" not in rendered
        assert "failed-invalid-batch" not in rendered

    def test_mark_invalid_from_failure_view_persists_reason(self, mutable_client):
        store, client = mutable_client
        callback_map = self._callback_map(client)
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs([str(SWEEP_A)]),
            self._failed_state("bad shards"),
            changed=[f'{{"failed-invalid": "{SWEEP_A}"}}.n_clicks'],
            group_ids=[str(SWEEP_A)],
        )
        row = store._curation_row(str(SWEEP_A))
        assert row[1] is not None and row[2] == "bad shards"
        children = str(response["failed-trials-panel"]["children"])
        assert "Marked invalid" in children
        row_ids = [entry["sweep_id"] for entry in response["sweep-grid"]["rowData"]]
        assert row_ids == [str(SWEEP_A), str(SWEEP_B)]
        grid_row = next(
            entry
            for entry in response["sweep-grid"]["rowData"]
            if entry["sweep_id"] == str(SWEEP_A)
        )
        assert grid_row["invalid"] is True
        note = str(response["workspace-curation-note"]["children"])
        assert "alpha is invalid" in note

    def test_mark_invalid_without_reason_is_rejected(self, mutable_client):
        store, client = mutable_client
        callback_map = self._callback_map(client)
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs([str(SWEEP_A)]),
            self._failed_state("   "),
            changed=[f'{{"failed-invalid": "{SWEEP_A}"}}.n_clicks'],
            group_ids=[str(SWEEP_A)],
        )
        children = str(response["failed-trials-panel"]["children"])
        assert "requires a reason" in children
        assert store._curation_row(str(SWEEP_A))[1] is None

    def test_batch_mark_invalid_invalidates_every_checked_sweep(self, mutable_client):
        """The shared-bug case: one click invalidates every checked sweep
        through a single apply_curation call carrying the full id list."""
        store, client = mutable_client
        self._fail_sweep_b(store)
        service = DashboardService(QueryService(store))
        groups = [str(SWEEP_A), str(SWEEP_B)]
        callback_map = self._callback_map(client)
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs(groups, batch=1, checked=set(groups)),
            self._failed_state("bad shards"),
            changed=["failed-invalid-batch.n_clicks"],
            group_ids=groups,
        )
        for sweep_id in (SWEEP_A, SWEEP_B):
            row = store._curation_row(str(sweep_id))
            assert row[1] is not None and row[2] == "bad shards"
        children = str(response["failed-trials-panel"]["children"])
        assert "Marked invalid alpha, beta." in children
        row_ids = [entry["sweep_id"] for entry in response["sweep-grid"]["rowData"]]
        assert row_ids == [str(SWEEP_A)]  # completed beta left discovery
        assert response["sweep-grid"]["selectedRows"] == []
        note = str(response["workspace-curation-note"]["children"])
        assert "alpha is invalid" in note

    def test_batch_mark_invalid_without_reason_is_rejected(self, mutable_client):
        store, client = mutable_client
        self._fail_sweep_b(store)
        service = DashboardService(QueryService(store))
        groups = [str(SWEEP_A), str(SWEEP_B)]
        callback_map = self._callback_map(client)
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs(groups, batch=1, checked=set(groups)),
            self._failed_state("   "),
            changed=["failed-invalid-batch.n_clicks"],
            group_ids=groups,
        )
        children = str(response["failed-trials-panel"]["children"])
        assert "requires a reason" in children
        assert store._curation_row(str(SWEEP_A))[1] is None
        assert store._curation_row(str(SWEEP_B))[1] is None

    def test_batch_mark_invalid_with_empty_selection_prompts(self, mutable_client):
        store, client = mutable_client
        self._fail_sweep_b(store)
        service = DashboardService(QueryService(store))
        groups = [str(SWEEP_A), str(SWEEP_B)]
        callback_map = self._callback_map(client)
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs(groups, batch=1),
            self._failed_state("bad shards"),
            changed=["failed-invalid-batch.n_clicks"],
            group_ids=groups,
        )
        children = str(response["failed-trials-panel"]["children"])
        assert "Select sweeps first" in children
        assert store._curation_row(str(SWEEP_A))[1] is None
        assert store._curation_row(str(SWEEP_B))[1] is None
        assert "sweep-grid" not in str(response)

    def test_select_all_mirrors_onto_group_checklists(self, mutable_client):
        store, client = mutable_client
        self._fail_sweep_b(store)
        service = DashboardService(QueryService(store))
        groups = [str(SWEEP_A), str(SWEEP_B)]
        callback_map = self._callback_map(client)
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs(groups, select_all=["all"]),
            self._failed_state(""),
            changed=["failed-select-all.value"],
            group_ids=groups,
        )
        assert set(response) == {f'{{"failed-sweep":"{sid}"}}' for sid in groups}
        for sid in groups:
            assert response[f'{{"failed-sweep":"{sid}"}}'] == {"value": [sid]}
        response = self._failed_dispatch(
            client,
            callback_map,
            self._failed_inputs(groups, select_all=[]),
            self._failed_state(""),
            changed=["failed-select-all.value"],
            group_ids=groups,
        )
        for sid in groups:
            assert response[f'{{"failed-sweep":"{sid}"}}'] == {"value": []}

    def test_checkbox_refire_actuates_nothing(self, mutable_client):
        """A checklist write or panel re-render remount re-fires the ALL
        input; only an explicit control may act."""
        store, client = mutable_client
        self._fail_sweep_b(store)
        groups = [str(SWEEP_A), str(SWEEP_B)]
        callback_map = self._callback_map(client)
        response = client.post(
            "/dashboard/_dash-update-component",
            json=self._failed_payload(
                callback_map,
                self._failed_inputs(groups, checked={str(SWEEP_A)}),
                self._failed_state("bad shards"),
                [f'{{"failed-sweep": "{SWEEP_A}"}}.value'],
                groups,
            ),
        )
        assert response.status_code == 204  # PreventUpdate
        assert store._curation_row(str(SWEEP_A))[1] is None

    def test_selected_failed_sweeps_flattens_checked_values(self):
        assert selected_failed_sweeps([]) == []
        assert selected_failed_sweeps([[], []]) == []
        assert selected_failed_sweeps([[], [str(SWEEP_A)], [str(SWEEP_B)]]) == [
            str(SWEEP_A),
            str(SWEEP_B),
        ]


def _find_pres(node: Component) -> list[html.Pre]:
    """Every Pre under ``node``, depth-first in render order."""
    return [child for child in _walk(node) if isinstance(child, html.Pre)]


class TestHeaderTraySummary:
    """jernerics-0h6: the tray line hides when empty, pluralizes truly."""

    def test_no_selection_yields_an_empty_placeholder(self):
        assert tray_summary(None) == ""
        assert tray_summary({}) == ""
        empty = {"sweeps": [], "trials": [], "families": [], "executions": []}
        assert tray_summary(empty) == ""

    @pytest.mark.parametrize(
        ("field", "one_line", "two_line"),
        [
            (
                "sweeps",
                "1 sweep · 0 trials · 0 families",
                "2 sweeps · 0 trials · 0 families",
            ),
            (
                "trials",
                "0 sweeps · 1 trial · 0 families",
                "0 sweeps · 2 trials · 0 families",
            ),
            (
                "families",
                "0 sweeps · 0 trials · 1 family",
                "0 sweeps · 0 trials · 2 families",
            ),
        ],
    )
    def test_each_dimension_pluralizes_for_one_and_two(self, field, one_line, two_line):
        for count, line in ((1, one_line), (2, two_line)):
            tray = {field: [f"id{i}" for i in range(count)]}
            assert tray_summary(tray) == line

    def test_singular_and_plural_forms_mix(self):
        tray = {"sweeps": ["a"], "trials": ["b"], "families": ["c"]}
        assert tray_summary(tray) == "1 sweep · 1 trial · 1 family"
        tray = {
            "sweeps": ["a", "b"],
            "trials": [f"t{i}" for i in range(14)],
            "families": ["e", "f", "g"],
        }
        assert tray_summary(tray) == "2 sweeps · 14 trials · 3 families"

    def test_executions_and_expand_append_their_segments(self):
        assert tray_summary({"executions": ["e1"]}) == (
            "0 sweeps · 0 trials · 0 families · 1 execution"
        )
        tray = {"sweeps": ["a"], "executions": ["e1", "e2"], "expand": True}
        assert tray_summary(tray) == (
            "1 sweep · 0 trials · 0 families · 2 executions · retry families expanded"
        )


class TestPythonHandoffSnippet:
    """jernerics-ui9: origin-derived URL, one statement per line."""

    def test_builder_emits_the_passed_base_url(self):
        for base_url in (
            "http://localhost:8000",
            "https://track.internal.example:8443",
            "http://10.0.0.7:9999",
        ):
            assert f'TrackingClient("{base_url}")' in python_snippet(
                "abc123", "ops", base_url
            )

    def test_client_instantiation_stays_on_one_line(self):
        token = "t" * 200
        base_url = "https://track.internal.example:8443"
        snippet = python_snippet(token, "ops", base_url)
        client_lines = [
            line for line in snippet.split("\n") if "TrackingClient(" in line
        ]
        assert client_lines == [f'client = TrackingClient("{base_url}")']

    def test_tab_points_the_client_at_the_browser_origin(self, service):
        tray = {"sweeps": [str(SWEEP_A)], "trials": [], "families": []}
        page = python_tab(service, "ops", tray, "http://127.0.0.1:8899")
        snippet = _find_pres(page)[1].children
        assert snippet is not None
        assert 'TrackingClient("http://127.0.0.1:8899")' in snippet

    def test_tab_pres_keep_statements_on_one_copyable_line(self, service):
        tray = {"sweeps": [str(SWEEP_A)], "trials": [], "families": []}
        page = python_tab(service, "ops", tray, "http://127.0.0.1:8899")
        pres = _find_pres(page)
        assert len(pres) == 2
        for pre in pres:
            style = dict(getattr(pre, "style", None) or {})
            assert style["whiteSpace"] == "pre"
            assert style["overflowX"] == "auto"


class TestOriginFromHref:
    def test_derives_the_scheme_and_netloc(self):
        href = "http://127.0.0.1:8899/dashboard/project/lab?view=x"
        assert origin_from_href(href) == "http://127.0.0.1:8899"
        assert origin_from_href("https://tracks.example.com/") == (
            "https://tracks.example.com"
        )

    def test_blank_or_relative_hrefs_fall_back_to_local_host(self):
        assert origin_from_href(None) == "http://localhost:8000"
        assert origin_from_href("") == "http://localhost:8000"
        assert origin_from_href("/dashboard/project/lab") == "http://localhost:8000"


class TestMountedTrayAndPythonCallbacks:
    """The registered shell/analysis callbacks, driven through Dash's
    dispatch endpoint exactly as the browser would."""

    _TRAY_OUTPUTS = {
        "selection-tray.children",
        "selection-tray.style",
    }

    def _callback_key(self, callback_map, wanted: set[str]) -> str:
        def outputs_of(key):
            stripped = key.removeprefix("..").removesuffix("..")
            return {part.split("@")[0] for part in stripped.split("...") if part}

        return next(key for key in callback_map if outputs_of(key) == wanted)

    def _dispatch(self, client, callback_map, wanted, inputs, state=()):
        key = self._callback_key(callback_map, wanted)
        specs = [
            part.split("@")[0]
            for part in key.removeprefix("..").removesuffix("..").split("...")
            if part
        ]
        outputs = [
            {"id": spec.split(".")[0], "property": spec.split(".")[1]} for spec in specs
        ]
        response = client.post(
            "/dashboard/_dash-update-component",
            json={
                "output": key,
                "outputs": outputs[0] if len(outputs) == 1 else outputs,
                "inputs": inputs,
                "state": list(state),
                "changedPropIds": [f"{i['id']}.{i['property']}" for i in inputs],
            },
        )
        assert response.status_code == 200, response.text
        return response.json()["response"]

    def _callback_map(self, client):
        from jernerics_server.dashboard.app import build_dash_app

        ctx = client.app.state.dashboard
        return build_dash_app(ctx).callback_map

    def test_shell_tray_button_starts_hidden(self):
        button = next(
            child
            for child in _walk(layout.shell())
            if getattr(child, "id", None) == "selection-tray"
        )
        assert button.style == {"display": "none"}

    def test_empty_selection_hides_the_tray(self, mutable_client):
        _store, client = mutable_client
        response = self._dispatch(
            client,
            self._callback_map(client),
            self._TRAY_OUTPUTS,
            [
                {
                    "id": "view-store",
                    "property": "data",
                    "value": {"scope": {"sweeps": [], "trials": [], "families": []}},
                },
                {"id": "project-store", "property": "data", "value": "ops"},
            ],
        )
        assert response["selection-tray"]["children"] == ""
        assert response["selection-tray"]["style"] == {"display": "none"}

    def test_selection_shows_plural_counts_and_restores_the_button(
        self, mutable_client
    ):
        _store, client = mutable_client
        response = self._dispatch(
            client,
            self._callback_map(client),
            self._TRAY_OUTPUTS,
            [
                {
                    "id": "view-store",
                    "property": "data",
                    "value": {"scope": {"sweeps": [str(SWEEP_A), str(SWEEP_B)]}},
                },
                {"id": "project-store", "property": "data", "value": "ops"},
            ],
        )
        assert response["selection-tray"]["children"] == (
            "2 sweeps · 0 trials · 0 families"
        )
        assert response["selection-tray"]["style"] == {}

    def test_python_tab_snippet_targets_the_browser_origin(self, mutable_client):
        _store, client = mutable_client
        response = self._dispatch(
            client,
            self._callback_map(client),
            {"analysis-python.children"},
            [
                {
                    "id": "view-store",
                    "property": "data",
                    "value": {"scope": {"sweeps": [str(SWEEP_A)], "trials": []}},
                },
                {"id": "analysis-tabs", "property": "value", "value": "python"},
                {
                    "id": "url",
                    "property": "href",
                    "value": "http://track.internal:9000/dashboard/project/ops",
                },
            ],
            state=[{"id": "project-store", "property": "data", "value": "ops"}],
        )
        rendered = str(response)
        assert 'TrackingClient("http://track.internal:9000")' in rendered
        assert "localhost:8000" not in rendered
