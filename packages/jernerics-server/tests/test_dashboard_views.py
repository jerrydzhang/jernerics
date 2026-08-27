"""Operational monitoring views (jernerics-h5d.12).

Callback-layer coverage over a seeded v3 store: the orchestrator
browser-drives the mounted dashboard after merge, so these tests assert
on the pure helpers the Dash callbacks wrap plus TestClient page 200s.
"""

import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from dash.development.base_component import Component
from dash_ag_grid import AgGrid
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionProgressEvent,
    ExecutionStartEvent,
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
from jernerics_server.dashboard.callbacks import (
    lineage_panel,
    page_content,
    tray_from_grid,
)
from jernerics_server.dashboard.components import grid_options, short_id
from jernerics_server.dashboard.layout import family_grid_row, sweep_grid_row
from jernerics_server.dashboard.service import DashboardService
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


def _walk(component: Component):
    yield component
    children = getattr(component, "children", None)
    if isinstance(children, Component):
        yield from _walk(children)
    elif isinstance(children, list | tuple):
        for child in children:
            if isinstance(child, Component):
                yield from _walk(child)


def _grid(page: Any, grid_id: str) -> Any:
    found = [
        node for node in _walk(page) if isinstance(node, AgGrid) and node.id == grid_id
    ]
    assert found, f"{grid_id} missing from page"
    return found[0]


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


class TestSweepGrid:
    def test_counts_backend_and_health_per_sweep(self, service):
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        alpha = summaries["alpha"]
        assert alpha.submitted_jobs == 2
        assert alpha.expected_trials == 8
        assert alpha.started == 6
        assert alpha.terminal == 2
        assert alpha.backend == "slurm"
        assert alpha.failed == 1
        assert alpha.health == "failing"
        beta = summaries["beta"]
        assert beta.submitted_jobs == 0
        assert beta.expected_trials == 1
        assert beta.started == 1
        assert beta.terminal == 1
        assert beta.backend == "local"
        assert beta.health == "healthy"

    def test_grid_rows_show_unknown_optimizer_and_direction(self, service):
        alpha = next(
            row for row in service.sweep_overview("ops") if row.name == "alpha"
        )
        row = sweep_grid_row(alpha, time.time_ns())
        assert row["optimizer"] == "—"
        assert row["health"] == "failing"
        assert row["latest_submission"] == "10m ago"

    def test_rendered_workspace_links_every_sweep_page(self, service):
        page, _ = page_content("/dashboard/project/ops", service)
        rendered = str(page)
        for summary in service.sweep_overview("ops"):
            link = f"[{summary.name}](/dashboard/sweep/{summary.sweep_id})"
            assert link in rendered

    def test_sweep_column_links_without_a_second_checkbox(self, service):
        """jernerics-sey: AG Grid 35's rowSelection multiRow already
        renders the selection column; a columnDef checkboxSelection
        duplicated it (two checkboxes per row)."""
        page, _ = page_content("/dashboard/project/ops", service)
        sweep_column = _grid(page, "sweep-grid").columnDefs[0]
        assert sweep_column["field"] == "name"
        assert sweep_column["cellRenderer"] == "markdown"
        assert "checkboxSelection" not in sweep_column
        assert "headerCheckboxSelection" not in sweep_column


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

    def test_sweep_and_family_grids_stay_selectable(self, service):
        workspace, _ = page_content("/dashboard/project/ops", service)
        sweep_options = _grid(workspace, "sweep-grid").dashGridOptions
        assert sweep_options["enableCellTextSelection"] is True
        assert sweep_options["ensureDomOrder"] is True
        assert sweep_options["rowSelection"] == {"mode": "multiRow"}

        sweep_page, _ = page_content(f"/dashboard/sweep/{SWEEP_A}", service)
        family_options = _grid(sweep_page, "family-grid").dashGridOptions
        assert family_options["enableCellTextSelection"] is True
        assert family_options["ensureDomOrder"] is True
        assert family_options["rowSelection"] == {"mode": "singleRow"}


class TestSweepPage:
    def test_job_correlation_rows(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        jobs = {(job["scheduler_job_id"], job["role"]) for job in detail.jobs}
        assert jobs == {("9400001", "trials"), ("9400002", "checker")}
        assert {job["backend"] for job in detail.jobs} == {"slurm"}
        page, _ = page_content(f"/dashboard/sweep/{SWEEP_A}", service)
        rendered = str(page)
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
        page, _ = page_content(f"/dashboard/sweep/{SWEEP_A}", service)
        rendered = str(page)
        for label in ("active", "quiet", "stale", "failed", "succeeded"):
            assert f"{label} 1" in rendered
        assert "unknown 1" in rendered

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
        page, _ = page_content(f"/dashboard/sweep/{SWEEP_A}", service)
        assert "7/10 epoch" in str(page)
        assert "3/10 epoch" in str(page)

    def test_executions_section_lists_and_links_every_execution(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        assert {str(record.execution_id) for record in detail.executions} == {
            str(execution_id) for execution_id in (E1, E3, E4, E5, E6, E7)
        }
        page, _ = page_content(f"/dashboard/sweep/{SWEEP_A}", service)
        rendered = str(page)
        assert "Executions" in rendered
        for execution_id in (E1, E3, E4, E5, E6, E7):
            assert f"/dashboard/execution/{execution_id}" in rendered
        assert f"/dashboard/execution/{E8}" not in rendered
        assert "node07" not in rendered
        finished = service.sweep_detail(str(SWEEP_B))
        assert finished is not None
        assert {str(record.execution_id) for record in finished.executions} == {str(E8)}
        finished_page, _ = page_content(f"/dashboard/sweep/{SWEEP_B}", service)
        assert f"/dashboard/execution/{E8}" in str(finished_page)
        assert "node07" in str(finished_page)

    def test_header_breadcrumbs_to_project_workspace(self, service):
        page, _ = page_content(f"/dashboard/sweep/{SWEEP_A}", service)
        rendered = str(page)
        assert "project ops" in rendered
        assert "/dashboard/project/ops" in rendered


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

    def test_family_cells_link_root_and_current_trials(self, service):
        detail = service.sweep_detail(str(SWEEP_A))
        assert detail is not None
        for family in detail.families:
            row = family_grid_row(family)
            assert row["root"] == family.root
            assert row["current_trial"] == family.current_trial
            assert row["root_short"] == (
                f"[{short_id(family.root)}](/dashboard/trial/{family.root})"
            )
            assert row["current_short"] == (
                f"[{short_id(family.current_trial)}]"
                f"(/dashboard/trial/{family.current_trial})"
            )
        page, _ = page_content(f"/dashboard/sweep/{SWEEP_A}", service)
        rendered = str(page)
        for family in detail.families:
            assert f"/dashboard/trial/{family.current_trial}" in rendered
        columns = {
            column["field"]: column for column in _grid(page, "family-grid").columnDefs
        }
        assert columns["root_short"]["cellRenderer"] == "markdown"
        assert columns["current_short"]["cellRenderer"] == "markdown"

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

    def test_tray_from_grid_keeps_project_and_analysis_picks(self):
        tray = tray_from_grid(
            [{"sweep_id": str(SWEEP_B)}, {"sweep_id": str(SWEEP_A)}],
            {
                "project": "ops",
                "sweeps": [],
                "trials": [str(T4)],
                "families": [str(F0)],
                "executions": [],
                "expand": True,
            },
        )
        assert tray == {
            "project": "ops",
            "sweeps": [str(SWEEP_A), str(SWEEP_B)],
            "trials": [str(T4)],
            "families": [str(F0)],
            "executions": [],
            "expand": True,
        }


class TestTrialPage:
    def test_family_header_params_catalog_and_executions(self, service):
        page, polls = page_content(f"/dashboard/trial/{F2}", service)
        rendered = str(page)
        assert "cc110000 → cc120000 → cc130000" in rendered
        assert "retry index 2" in rendered
        assert "completed" in rendered
        assert "objective 0.75" in rendered
        assert "sampled" in rendered and "manual" in rendered
        assert "loss" in rendered
        assert "node01" in rendered and "node02" in rendered
        assert f"/dashboard/execution/{E1}" in rendered
        assert f"/dashboard/execution/{E3}" in rendered
        assert polls is False


class TestExecutionPage:
    def test_timeline_failure_summary_and_separate_sections(self, service):
        page, _polls = page_content(f"/dashboard/execution/{E1}", service)
        rendered = str(page)
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
        page, polls = page_content(f"/dashboard/execution/{E4}", service)
        rendered = str(page)
        assert polls is True
        assert "7/10 epoch" in rendered
        assert "host node03" in rendered
        assert '"lr": 0.1' in rendered
        assert "deadbeef" in rendered
        assert "sweep.yaml" in rendered
        assert "slurm" in rendered
        assert "section-optimizer-state" in rendered
        assert "running" in rendered

    def test_stale_execution_renders_stale_not_failed(self, service):
        page, _ = page_content(f"/dashboard/execution/{E6}", service)
        rendered = str(page)
        assert "badge-stale" in rendered
        assert "Span(children='stale'" in rendered
        assert "failed" not in rendered

    def test_missing_heartbeat_renders_unknown(self, service):
        page, _ = page_content(f"/dashboard/execution/{E7}", service)
        rendered = str(page)
        assert "badge-unknown" in rendered
        assert "Span(children='unknown'" in rendered
        assert "Last heartbeat" in rendered


class TestPolling:
    def test_interval_enabled_only_while_work_incomplete(self, service):
        assert page_content("/dashboard/project/ops", service)[1] is True
        assert page_content(f"/dashboard/sweep/{SWEEP_A}", service)[1] is True
        assert page_content(f"/dashboard/sweep/{SWEEP_B}", service)[1] is False
        assert page_content(f"/dashboard/trial/{T4}", service)[1] is True
        assert page_content(f"/dashboard/trial/{F2}", service)[1] is False
        assert page_content(f"/dashboard/execution/{E4}", service)[1] is True
        assert page_content(f"/dashboard/execution/{E8}", service)[1] is False


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
            f"/dashboard/sweep/{SWEEP_A}",
            f"/dashboard/sweep/{SWEEP_B}",
            f"/dashboard/trial/{F2}",
            f"/dashboard/execution/{E1}",
            f"/dashboard/execution/{E4}",
        ):
            response = authed.get(url)
            assert response.status_code == 200, url
            assert "react-entry-point" in response.text
