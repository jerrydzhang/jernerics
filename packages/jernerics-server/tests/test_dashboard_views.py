"""Overview browser and curation coverage.

Callback-layer coverage over a seeded v3 store: the orchestrator
browser-drives the mounted dashboard after merge, so these tests assert
on the pure helpers the Dash callbacks wrap plus TestClient page 200s.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from jernerics_server.dashboard.analysis import (
    python_snippet,
)
from jernerics_server.dashboard.callbacks import (
    page_content,
)
from jernerics_server.dashboard.components import (
    MISSING,
)
from jernerics_server.dashboard.render import (
    SortColumn,
    sort_rows,
    sortable_columns,
    typed_sort_key,
)
from jernerics_server.dashboard.routes import ROUTES_BASE
from jernerics_server.dashboard.service import (
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
)
from jernerics_server.dashboard.workspace import (
    best_objective_text,
    overview_filter_passes,
    overview_row,
)
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store

API_KEY = "secret123"

SWEEP_A = uuid.UUID("aa110000-0000-4000-8000-000000000000")
SWEEP_B = uuid.UUID("aa220000-0000-4000-8000-000000000000")
SWEEP_C = uuid.UUID("aa230000-0000-4000-8000-000000000000")
SWEEP_D = uuid.UUID("aa240000-0000-4000-8000-000000000000")
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


ST_OK = uuid.UUID("aa410000-0000-4000-8000-000000000001")
ST_NOOBJ = uuid.UUID("aa420000-0000-4000-8000-000000000002")
ST_FAIL = uuid.UUID("aa430000-0000-4000-8000-000000000003")
ST_MIX = uuid.UUID("aa440000-0000-4000-8000-000000000004")
ST_TERMINAL = uuid.UUID("aa450000-0000-4000-8000-000000000005")
ST_T0 = uuid.UUID("cc410000-0000-4000-8000-000000000000")
ST_T1 = uuid.UUID("cc410000-0000-4000-8000-000000000001")
ST_T2 = uuid.UUID("cc420000-0000-4000-8000-000000000000")
ST_T3 = uuid.UUID("cc430000-0000-4000-8000-000000000000")
ST_T4 = uuid.UUID("cc440000-0000-4000-8000-000000000000")
ST_T5 = uuid.UUID("cc440000-0000-4000-8000-000000000001")
ST_T6 = uuid.UUID("cc450000-0000-4000-8000-000000000000")
ST_E0 = uuid.UUID("dd410000-0000-4000-8000-000000000000")
ST_E1 = uuid.UUID("dd410000-0000-4000-8000-000000000001")
ST_E2 = uuid.UUID("dd420000-0000-4000-8000-000000000000")
ST_E3 = uuid.UUID("dd430000-0000-4000-8000-000000000000")
ST_E4 = uuid.UUID("dd440000-0000-4000-8000-000000000000")
ST_E5 = uuid.UUID("dd440000-0000-4000-8000-000000000001")
ST_E6 = uuid.UUID("dd450000-0000-4000-8000-000000000000")


def _status_seed_events() -> list:
    """One project whose sweeps all still announce "running" from their
    snapshot events: an all-terminal sweep with objectives, a completed
    trial without one, and trials whose failure only shows in their
    execution outcomes (stuck "running" or terminal-failed)."""
    now = datetime.now(UTC)

    def at(seconds_ago: float) -> datetime:
        return now - timedelta(seconds=seconds_ago)

    def event(cls, seconds_ago: float, **kwargs):
        return cls(event_id=uuid.uuid4(), recorded_at=at(seconds_ago), **kwargs)

    def sweep(sweep_id, name, trial_specs):
        events = [
            event(
                SweepSnapshotEvent,
                900,
                project="status",
                sweep_id=sweep_id,
                name=name,
                state="running",
            ),
            event(
                SubmissionSnapshotEvent,
                895,
                submission_id=uuid.uuid5(uuid.NAMESPACE_OID, name),
                sweep_id=sweep_id,
                backend="local",
                state="completed",
                submitted_at=at(895),
            ),
        ]
        for index, (trial_id, execution_id, state, objective, outcome) in enumerate(
            trial_specs
        ):
            events.append(
                event(
                    TrialSnapshotEvent,
                    800 - index,
                    trial_id=trial_id,
                    sweep_id=sweep_id,
                    number=index,
                    state=state,
                    retry_root_trial_id=trial_id,
                    objective=objective,
                )
            )
            events.append(
                event(
                    ExecutionStartEvent,
                    700 - index,
                    execution_id=execution_id,
                    trial_id=trial_id,
                    hostname="node00",
                    started_at=at(700 - index),
                )
            )
            if outcome is not None:
                events.append(
                    event(
                        ExecutionEndEvent,
                        600 - index,
                        execution_id=execution_id,
                        ended_at=at(600 - index),
                        outcome=outcome,
                        exit_code=0 if outcome == "success" else 1,
                    )
                )
        return events

    return [
        *sweep(
            ST_OK,
            "all-done",
            [
                (ST_T0, ST_E0, TrialState.COMPLETED, 0.07, "success"),
                (ST_T1, ST_E1, TrialState.COMPLETED, 0.05, "success"),
            ],
        ),
        *sweep(
            ST_NOOBJ,
            "no-objective",
            [(ST_T2, ST_E2, TrialState.COMPLETED, None, "success")],
        ),
        *sweep(
            ST_FAIL,
            "exec-failed",
            [(ST_T3, ST_E3, TrialState.RUNNING, None, "failure")],
        ),
        *sweep(
            ST_MIX,
            "mixed",
            [
                (ST_T4, ST_E4, TrialState.COMPLETED, 0.2, "success"),
                (ST_T5, ST_E5, TrialState.RUNNING, None, "failure"),
            ],
        ),
        *sweep(
            ST_TERMINAL,
            "terminal-failed",
            [(ST_T6, ST_E6, TrialState.FAILED, None, "failure")],
        ),
    ]


@pytest.fixture
def status_service(tmp_path) -> DashboardService:
    store = Store(tmp_path / "status.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_status_seed_events())
    )
    assert not result.conflicts
    return DashboardService(QueryService(store))


class TestSweepStatusAndBestObjective:
    def test_project_table_derives_completed_from_trials(self, status_service):
        summaries = {row.name: row for row in status_service.sweep_overview("status")}
        assert summaries["all-done"].state == "completed"
        assert summaries["no-objective"].state == "completed"

    def test_best_objective_is_the_real_minimum(self, status_service):
        summaries = {row.name: row for row in status_service.sweep_overview("status")}
        assert best_objective_text(summaries["all-done"]) == "0.05"

    def test_best_objective_renders_em_dash_without_one(self, status_service):
        summaries = {row.name: row for row in status_service.sweep_overview("status")}
        assert best_objective_text(summaries["no-objective"]) == MISSING

    def test_overview_row_cells_render_derived_facts(self, status_service):
        summaries = {row.name: row for row in status_service.sweep_overview("status")}
        cells = overview_row("status", summaries["all-done"], "", 0).children
        assert cells[3].children == "2/2"
        assert cells[4].children == "0.05"
        dot = cells[2].children
        assert getattr(dot, "className", None) == "st st-completed"

    def test_overview_row_renders_em_dash_cell(self, status_service):
        summaries = {row.name: row for row in status_service.sweep_overview("status")}
        cells = overview_row("status", summaries["no-objective"], "", 0).children
        assert cells[4].children == MISSING

    def test_failed_execution_marks_sweep_failed(self, status_service):
        summaries = {row.name: row for row in status_service.sweep_overview("status")}
        assert summaries["exec-failed"].state == "failed"
        assert summaries["mixed"].state == "failed"
        assert summaries["terminal-failed"].state == "failed"

    def test_failed_counts_are_trial_effective(self, status_service):
        summaries = {row.name: row for row in status_service.sweep_overview("status")}
        assert summaries["exec-failed"].trials_failed == 1
        assert summaries["mixed"].trials_complete == 1
        assert summaries["mixed"].trials_failed == 1


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


def _cls(node) -> str | None:
    if not hasattr(node, "to_plotly_json"):
        return None
    props = node.to_plotly_json().get("props", {})
    return props.get("class_name", props.get("className"))


def _walk(component: Component):
    yield component
    children = getattr(component, "children", None)
    if isinstance(children, Component | str):
        yield from _walk(children)
    elif isinstance(children, list | tuple):
        for child in children:
            yield from _walk(child)


NOW = 0


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


class TestOverviewPage:
    """The rebuilt project Overview: heading, scope line, working tiles,
    and one Sweeps section - a single paginated sortable table whose
    checkboxes feed Create Investigation (rewrite epic jernerics-xjxa)."""

    def _page(self, service, project="ops", search=None):
        page, polls = page_content(
            f"{ROUTES_BASE}/project/{project}", service, search=search
        )
        return page, polls, str(page)

    def test_guards_for_missing_and_fully_curated_projects(self, store_and_service):
        store, service = store_and_service
        _page, _polls, text = self._page(service, project="ghost")
        assert "No sweeps tracked for project ghost yet." in text
        store.mark_sweep_invalid(str(SWEEP_A), "keep visible while incomplete")
        store.archive_sweep(str(SWEEP_A))
        _page, _polls, text = self._page(service)
        # the archived sweep is still incomplete, so it never drops
        assert "/sweep/" + str(SWEEP_A) in text
        assert "badge invalid" in text

    def test_overview_never_fetches_per_sweep_detail(self, service, monkeypatch):
        def forbidden(_self, _sweep_id):
            raise AssertionError("overview render must not call sweep_detail")

        monkeypatch.setattr(DashboardService, "sweep_detail", forbidden)
        _page, _polls, text = self._page(service)
        assert "Sweeps" in text

    def test_composition_is_heading_tiles_and_one_table(self, service):
        page, _polls, text = self._page(service)
        tables = [n for n in _walk(page) if isinstance(n, html.Table)]
        assert len(tables) == 1
        assert tables[0].className == "sortable"
        headings = [n for n in _walk(page) if isinstance(n, (html.H1, html.H2))]
        assert [h.children for h in headings] == ["Overview", "Sweeps"]
        assert "selbar" in text
        assert "failed-trials-view" not in text
        grids = [n for n in _walk(page) if isinstance(n, AgGrid)]
        assert grids == []

    def test_tiles_report_the_scope_facts(self, service):
        _page, _polls, text = self._page(service)
        assert "failed executions \u00b7 1 sweep" in text
        assert "interrupted runs" in text
        assert "completed sweeps" in text
        assert "sweeps with no trials yet" in text
        assert "tile crit" in text
        assert "tile warn" in text

    def test_every_tile_is_a_filter_link(self, service):
        page, _polls, _text = self._page(service)
        tiles = [
            n
            for n in _walk(page)
            if isinstance(n, html.A) and (_cls(n) or "").startswith("tile")
        ]
        assert [tile.href.split("?f=")[-1] for tile in tiles] == [
            "failed",
            "stale",
            "completed",
            "no-data",
        ]
        assert all(tile.href.startswith(f"{ROUTES_BASE}/project/ops") for tile in tiles)

    def test_tile_filters_narrow_the_table_and_show_a_way_back(self, service):
        page, _polls, text = self._page(service, search="?f=failed")
        assert "/sweep/" + str(SWEEP_A) in text
        assert "/sweep/" + str(SWEEP_B) not in text
        assert "1 sweep with failed executions" in text
        chip = [n for n in _walk(page) if _cls(n) == "chip"][0]
        remove = [n for n in _walk(chip) if isinstance(n, html.A)][0]
        assert "f=failed" not in remove.href
        page, _polls, text = self._page(service, search="?f=completed")
        assert "/sweep/" + str(SWEEP_B) in text
        assert "/sweep/" + str(SWEEP_A) not in text
        page, _polls, text = self._page(service, search="?f=no-data")
        assert "showing 0\u20130 of 0" in text
        assert "filtered from 2" in text
        page, _polls, text = self._page(service, search="?f=no-data&limit=all")
        assert "showing 0\u20130 of 0" in text

    def test_filter_predicate_matches_execution_facts_and_states(self, service):
        alpha = service.sweep_overview("ops")[0]
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        alpha = summaries["alpha"]
        beta = summaries["beta"]
        assert overview_filter_passes(alpha, None) is True
        assert overview_filter_passes(alpha, "failed") is True
        assert overview_filter_passes(alpha, "stale") is True
        assert overview_filter_passes(alpha, "completed") is False
        assert overview_filter_passes(beta, "completed") is True
        assert overview_filter_passes(beta, "failed") is False

    def test_active_all_control_reflects_the_include_flags(self, service):
        _page, _polls, text = self._page(service)
        assert "Active (2)" in text
        assert "All (2)" in text
        assert "Active sweeps · last activity" in text
        _page, _polls, text = self._page(service, search="?scope=all")
        assert "All sweeps" in text

    def test_table_rows_carry_the_prototype_cells(self, service):
        page, _polls, text = self._page(service)
        assert f"{ROUTES_BASE}/project/ops/sweep/{SWEEP_A}" in text
        assert "st st-stale" in text
        assert "1/8" in text
        assert "0.25" in text
        checkboxes = [
            n
            for n in _walk(page)
            if isinstance(n, dcc.Checklist) and "sel-sweep" in str(n.id)
        ]
        assert {str(cb.id["sel-sweep"]) for cb in checkboxes} == {
            str(SWEEP_A),
            str(SWEEP_B),
        }

    def test_shared_name_prefix_elides_into_the_link(self, store_and_service):
        store, service = store_and_service
        now = datetime.now(UTC)
        shared: list = [
            SweepSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=now - timedelta(seconds=40),
                project="pad",
                sweep_id=sweep_id,
                name=f"alpha_cfg_{tag}",
                state="running",
            )
            for sweep_id, tag in ((SWEEP_C, "one"), (SWEEP_D, "two"))
        ]
        result = IngestService(store).apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=shared)
        )
        assert not result.conflicts
        page, _polls, _text = self._page(service, project="pad")
        prefixes = [
            n.children
            for n in _walk(page)
            if isinstance(n, html.Span) and n.className == "pfx"
        ]
        assert prefixes == ["alpha_cfg", "alpha_cfg"]

    def test_typed_sort_orders_rows_and_marks_the_header(self, service):
        page, _polls, _text = self._page(service, search="?sort=trials:desc")
        order = [
            str(n.href).rsplit("/", 1)[-1]
            for n in _walk(page)
            if isinstance(n, html.A) and _cls(n) == "sweep-link"
        ]
        assert order == [str(SWEEP_A), str(SWEEP_B)]
        heads = [
            n
            for n in _walk(page)
            if isinstance(n, html.Th)
            and n.to_plotly_json().get("props", {}).get("data-dir") == "desc"
        ]
        assert len(heads) == 1
        page, _polls, _text = self._page(service, search="?sort=best_objective:asc")
        order = [
            str(n.href).rsplit("/", 1)[-1]
            for n in _walk(page)
            if isinstance(n, html.A) and _cls(n) == "sweep-link"
        ]
        assert order == [str(SWEEP_B), str(SWEEP_A)]

    def test_pagination_slices_the_sorted_set(self, store_and_service):
        store, service = store_and_service
        now = datetime.now(UTC)
        events: list = [
            SweepSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=now - timedelta(seconds=40 + index),
                project="ops",
                sweep_id=uuid.uuid5(uuid.NAMESPACE_OID, f"pad-{index:02d}"),
                name=f"pad-{index:02d}",
                state="completed",
            )
            for index in range(28)
        ]
        result = IngestService(store).apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
        )
        assert not result.conflicts
        page, _polls, text = self._page(service)
        rows = [
            n for n in _walk(page) if isinstance(n, html.A) and _cls(n) == "sweep-link"
        ]
        assert len(rows) == 25
        assert "showing 1\u201325 of 30" in text
        pager = [n for n in _walk(page) if _cls(n) == "pager"][0]
        buttons = _walk_children_list(pager)
        assert [b.children for b in buttons] == ["\u2039", "1", "2", "\u203a"]
        page, _polls, text = self._page(service, search="?page=2")
        rows = [
            n for n in _walk(page) if isinstance(n, html.A) and _cls(n) == "sweep-link"
        ]
        assert len(rows) == 5
        assert "showing 26\u201330 of 30" in text
        page, _polls, text = self._page(service, search="?limit=all")
        rows = [
            n for n in _walk(page) if isinstance(n, html.A) and _cls(n) == "sweep-link"
        ]
        assert len(rows) == 30
        assert "showing 1\u201330 of 30" in text

    def test_sort_resets_and_covers_every_page(self, store_and_service):
        store, service = store_and_service
        now = datetime.now(UTC)
        events: list = [
            SweepSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=now - timedelta(seconds=40 + index),
                project="ops",
                sweep_id=uuid.uuid5(uuid.NAMESPACE_OID, f"pad-{index:02d}"),
                name=f"pad-{index:02d}",
                state="completed",
            )
            for index in range(28)
        ]
        IngestService(store).apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
        )
        page, _polls, _text = self._page(service, search="?sort=name:asc&page=2")
        rows = [
            n.children[-1].children
            for n in _walk(page)
            if isinstance(n, html.A) and _cls(n) == "sweep-link"
        ]
        assert rows[0] == "pad-23"
        assert rows[-1] == "pad-27"

    def test_selection_bar_mounts_hidden(self, service):
        page, _polls, _text = self._page(service)
        bar = [n for n in _walk(page) if getattr(n, "id", None) == "selbar"][0]
        assert bar.hidden is True
        create = [n for n in _walk(page) if getattr(n, "id", None) == "sel-create"][0]
        assert create.href == "#"
        assert [n for n in _walk(page) if getattr(n, "id", None) == "sel-clear"]

    def test_polls_follow_the_visible_scope(self, store_and_service):
        store, service = store_and_service
        _page, polls, _text = self._page(service)
        assert polls is True
        store.archive_sweep(str(SWEEP_A))
        summaries = {row.name: row for row in service.sweep_overview("ops")}
        assert summaries["alpha"].incomplete is True
        # the archived sweep is still incomplete, so it stays in Active
        # scope and keeps the page live
        _page, polls, text = self._page(service)
        assert polls is True
        assert "/sweep/" + str(SWEEP_A) in text
        _page, polls, _unused = self._page(service, search="?scope=all")
        assert polls is True


class TestOverviewCurationVisibility:
    """Active hides curated terminal sweeps until the All scope includes
    them; incomplete sweeps never drop, curated or not."""

    def test_archived_terminal_sweep_leaves_active_and_shows_its_footnote(
        self, store_and_service
    ):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        _page, _polls, text = self._page(service)
        assert "Active sweeps \u2014 hides 1 archived/invalid" in text
        assert "/sweep/" + str(SWEEP_B) not in text

    def test_all_scope_brings_the_archived_sweep_back(self, store_and_service):
        store, service = store_and_service
        store.archive_sweep(str(SWEEP_B))
        _page, _polls, text = self._page(service, search="?scope=all")
        assert "All sweeps \u2014 including 1 archived/invalid" in text
        assert "/sweep/" + str(SWEEP_B) in text
        assert "badge archived" in text

    def test_invalid_sweep_needs_the_all_scope_too(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_B), "contaminated dataset")
        _page, _polls, text = self._page(service)
        assert "/sweep/" + str(SWEEP_B) not in text
        _page, _polls, text = self._page(service, search="?scope=all")
        assert "badge invalid" in text

    def test_incomplete_curated_sweep_stays_visible(self, store_and_service):
        store, service = store_and_service
        store.mark_sweep_invalid(str(SWEEP_A), "still running it down")
        _page, _polls, text = self._page(service)
        assert "/sweep/" + str(SWEEP_A) in text
        assert "badge invalid" in text
        assert "Active sweeps" in text

    def test_fully_curated_project_names_the_curation(self, curated):
        store, service = curated
        store.archive_sweep(str(CUR_SWEEP_OLD))
        store.archive_sweep(str(CUR_SWEEP_NEW))
        _page, polls, text = self._page(service, project="curate")
        assert "No current sweeps in project curate" in text
        assert "failed executions" in text
        assert polls is False

    def _page(self, service, project="ops", search=None):
        page, polls = page_content(
            f"{ROUTES_BASE}/project/{project}", service, search=search
        )
        return page, polls, str(page)


def _walk_children_list(component):
    return list(component.children)


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
            "/dashboard/project/ops/investigation/new",
            "/dashboard/project/ops/investigation/abc-123",
            "/dashboard/project/ops/investigation/abc-123/edit",
        ):
            response = authed.get(url)
            assert response.status_code == 200, url
            assert "react-entry-point" in response.text

    def test_investigation_pages_without_a_store_stay_honest(self, service):
        page, polls = page_content("/dashboard/project/ops/investigation/new", service)
        assert "no write store" in str(page)
        assert polls is False
        page, _ = page_content("/dashboard/project/ops/investigation/abc-123", service)
        assert "no write store" in str(page)


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


def _find_pres(node: Component) -> list[html.Pre]:
    """Every Pre under ``node``, depth-first in render order."""
    return [child for child in _walk(node) if isinstance(child, html.Pre)]


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


class TestSortableTableInfrastructure:
    """One shared helper gives every grid typed sort keys (numeric, ns,
    string), missing values last in either direction, and one order
    over the whole row set regardless of pagination."""

    def test_typed_sort_key_kinds(self):
        assert typed_sort_key(3, "numeric") < typed_sort_key(10, "numeric")
        assert typed_sort_key(2.5, "ns") < typed_sort_key(7, "ns")
        assert typed_sort_key("alpha", "string") < typed_sort_key("beta", "string")
        assert typed_sort_key("Beta", "string") == typed_sort_key("beta", "string")
        assert typed_sort_key(10, "numeric") > typed_sort_key(9, "numeric")

    def test_typed_sort_key_missing_values_rank_last(self):
        present = typed_sort_key("x", "string")
        for missing in (None, "", MISSING):
            assert typed_sort_key(missing, "string") > present
            assert typed_sort_key(missing, "numeric") > typed_sort_key(-1e9, "numeric")

    def test_sort_rows_orders_the_whole_row_set_with_empties_last(self):
        columns = [SortColumn("value", "Value", "numeric")]
        rows = [
            {"id": n, "value": v}
            for n, (v,) in enumerate([(10,), (2,), (None,), (1,), ("—",), ("",), (33,)])
        ]
        ordered = sort_rows(rows, columns, [{"colId": "value", "sort": "asc"}])
        assert [row["value"] for row in ordered] == [1, 2, 10, 33, None, "—", ""]
        ordered = sort_rows(rows, columns, [{"colId": "value", "sort": "desc"}])
        assert [row["value"] for row in ordered] == [33, 10, 2, 1, None, "—", ""]

    def test_sort_rows_compares_ns_stamps_numerically(self):
        columns = [SortColumn("at_ns", "Last activity", "ns")]
        rows = [{"at_ns": ns} for ns in (300, 1_000_000_000, 5, None)]
        ordered = sort_rows(rows, columns, [{"colId": "at_ns", "sort": "desc"}])
        assert [row["at_ns"] for row in ordered] == [
            1_000_000_000,
            300,
            5,
            None,
        ]

    def test_sort_rows_sorts_text_as_text(self):
        columns = [SortColumn("name", "Sweep", "string")]
        rows = [{"name": name} for name in ("alpha10", "alpha2", "beta", None)]
        ordered = sort_rows(rows, columns, [{"colId": "name", "sort": "asc"}])
        assert [row["name"] for row in ordered] == ["alpha10", "alpha2", "beta", None]

    def test_sort_rows_ignores_unknown_or_directionless_entries(self):
        columns = [SortColumn("value", "Value", "numeric")]
        rows = [{"value": 2}, {"value": 1}]
        assert sort_rows(rows, columns, [{"colId": "ghost", "sort": "asc"}]) == rows
        assert sort_rows(rows, columns, [{"colId": "value", "sort": None}]) == rows
        assert sort_rows(rows, columns, None) == rows

    def test_sortable_columns_carry_typed_comparators_and_stored_sort(self):
        columns = [
            SortColumn("name", "Sweep", "string"),
            SortColumn(
                "last_activity_ns",
                "Last activity",
                "ns",
                definition={"valueFormatter": {"function": "renderRelative(x)"}},
            ),
        ]
        defs = sortable_columns(
            columns, [{"colId": "last_activity_ns", "sort": "desc"}]
        )
        assert defs[0]["comparator"] == {
            "function": "renderTypedSort('string', 'name')"
        }
        assert defs[1]["comparator"] == {
            "function": "renderTypedSort('ns', 'last_activity_ns')"
        }
        assert defs[1]["sort"] == "desc"
        assert "sort" not in defs[0]
        assert defs[1]["valueFormatter"] == {"function": "renderRelative(x)"}


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
        # alpha's failed execution stays Current: the health cell is a
        # status dot whose note carries the count.
        assert (
            "Span(children=['failed', Span(children='1', className='note')], "
            "className='st st-failed')" in rendered
        )


def _callback_key(callback_map, wanted: set[str]) -> str:
    def outputs_of(key):
        stripped = key.removeprefix("..").removesuffix("..")
        return {part.split("@")[0] for part in stripped.split("...") if part}

    return next(key for key in callback_map if outputs_of(key) == wanted)


def _dispatch(client, callback_map, wanted, inputs, state=(), changed=None):
    key = _callback_key(callback_map, wanted)
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
            "changedPropIds": changed or [f"{i['id']}.{i['property']}" for i in inputs],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["response"]


def _callback_map(client):
    from jernerics_server.dashboard.app import build_dash_app

    app = client.app
    ctx = app.state.dashboard
    return build_dash_app(ctx).callback_map


class TestExceptionsPage:
    """jernerics-cq78: the Exceptions page groups the scope-wide failed
    executions by cause, sweep, or host over Active/All segments; the
    working selection feeds one mark-invalid action, and ``?sweep=``
    pre-expands a deep-linked sweep's group."""

    _dispatch = staticmethod(_dispatch)
    _callback_key = staticmethod(_callback_key)
    _callback_map = staticmethod(_callback_map)

    _TRIAGE_OUTPUTS = {
        "exc-groupsets.children",
        "exc-note.children",
        "exc-selection-count.children",
    }

    def _triage_dispatch(self, client, callback_map, sweeps, reason):
        return self._dispatch(
            client,
            callback_map,
            self._TRIAGE_OUTPUTS,
            inputs=[
                {
                    "id": "exc-selection-store",
                    "property": "data",
                    "value": {"sweeps": sweeps, "reason": reason, "mode": "cause"},
                }
            ],
            state=[
                {
                    "id": "url",
                    "property": "pathname",
                    "value": f"{ROUTES_BASE}/project/ops/exceptions",
                },
                {"id": "url", "property": "search", "value": ""},
            ],
        )

    @staticmethod
    def _ingest_failure(store, trial_id: uuid.UUID) -> None:
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
                        trial_id=trial_id,
                        hostname="node01",
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

    def _fail_sweep_b(self, store) -> None:
        """A failed execution on beta's only trial (T9) so the ops scope
        carries two failed sweeps."""
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

    def test_service_lists_failed_executions_with_kind_and_summary(self, service):
        rows = service.failed_executions("ops")
        assert [(row.trial_number, row.failure_kind) for row in rows] == [
            (1, "exception")
        ]
        row = rows[0]
        assert row.sweep_id == str(SWEEP_A)
        assert row.sweep_name == "alpha"
        assert row.trial_id == str(F0)
        assert row.exit_code == 1
        assert row.hostname == "node01"
        assert row.failure_summary == "boom: divide by zero"

    def test_hidden_curated_sweep_failures_stay_out(self, curated):
        store, service = curated
        self._ingest_failure(store, CUR_T1)
        assert [row.failure_kind for row in service.failed_executions("curate")] == [
            "timeout"
        ]
        store.mark_sweep_invalid(str(CUR_SWEEP_OLD), "bad shard map")
        assert service.failed_executions("curate") == []
        historical = service.failed_executions("curate", include_curated=True)
        assert [row.failure_kind for row in historical] == ["timeout"]

    def test_active_page_groups_failures_by_cause_with_selection(self, service):
        page, polls = page_content(f"{ROUTES_BASE}/project/ops/exceptions", service)
        assert polls is False
        rendered = str(page)
        assert "1 failed executions · Active sweeps — not yet curated" in rendered
        assert f"href='{ROUTES_BASE}/project/ops/exceptions'" in rendered
        assert f"href='{ROUTES_BASE}/project/ops/exceptions?scope=all'" in rendered
        assert "exception · exit code 1" in rendered
        assert " — boom: divide by zero" in rendered
        assert "×1" in rendered
        assert "1 sweep" in rendered
        assert "node01" in rendered
        assert f"name='{SWEEP_A}'" in rendered  # the id the action reads
        assert "Mark invalid" in rendered
        assert "exc-reason" in rendered
        assert "exc-selection-count" in rendered
        assert rendered.count("hidden=True") == 2  # sweep and host sets
        assert "hidden=False" in rendered  # cause set starts visible

    def test_selection_checkboxes_render_in_summaries_with_marker_classes(
        self, service
    ):
        page, _ = page_content(f"{ROUTES_BASE}/project/ops/exceptions", service)
        markers = [
            node
            for node in _walk(page)
            if isinstance(node, dcc.Input)
            and str(getattr(node, "className", "")).startswith("sel-")
        ]
        summaries = [
            node
            for node in _walk(page)
            if isinstance(node, html.Summary)
            and any(
                isinstance(child, dcc.Input)
                and str(getattr(child, "className", "")).startswith("sel-")
                for child in node.children
            )
        ]
        assert len(markers) == len(summaries) == 6  # one group + sweep per groupset
        assert {box.name for box in markers if box.className == "sel-sweep"} == {
            str(SWEEP_A)
        }

    def test_stylesheet_sizes_the_selection_checkbox_wrappers(self, authed):
        css = authed.get(f"{ROUTES_BASE}/assets/page.css").text
        assert ".np details.failgroup summary .dash-input-container {" in css
        assert ".np details.failgroup summary .dash-input-container input" in css

    def test_all_three_groupsets_render_heads(self, service):
        page, _ = page_content(f"{ROUTES_BASE}/project/ops/exceptions", service)
        rendered = str(page)
        assert "By cause" in rendered
        assert "By sweep" in rendered
        assert "By host" in rendered
        assert "host node01" in rendered
        assert "alpha" in rendered

    def test_all_scope_includes_curated_failures_and_annotates(self, curated):
        store, service = curated
        self._ingest_failure(store, CUR_T1)
        store.mark_sweep_invalid(str(CUR_SWEEP_OLD), "bad shard map")
        active, _ = page_content(f"{ROUTES_BASE}/project/curate/exceptions", service)
        assert "killed after 3600s" not in str(active)
        all_page, _ = page_content(
            f"{ROUTES_BASE}/project/curate/exceptions",
            service,
            search="?scope=all",
        )
        rendered = str(all_page)
        assert "All sweeps — historical" in rendered
        assert "killed after 3600s" in rendered
        assert "active excludes 1 curated sweeps" in rendered
        assert "badge invalid" in rendered

    def test_deep_link_pre_expands_the_sweep_group(self, service):
        linked, _ = page_content(
            f"{ROUTES_BASE}/project/ops/exceptions",
            service,
            search=f"?sweep={SWEEP_A}",
        )
        details = [
            node
            for node in _walk(linked)
            if isinstance(node, html.Details)
            and getattr(node, "id", None) == f"sweep-{SWEEP_A}"
        ]
        assert details
        assert all(getattr(node, "open", False) is True for node in details)
        plain, _ = page_content(f"{ROUTES_BASE}/project/ops/exceptions", service)
        collapsed = [
            node
            for node in _walk(plain)
            if isinstance(node, html.Details)
            and getattr(node, "id", None) == f"sweep-{SWEEP_A}"
        ]
        assert collapsed
        assert not any(getattr(node, "open", False) for node in collapsed)

    def test_page_without_failures_renders_empty_rollup(self, curated):
        _store, service = curated
        page, _ = page_content(f"{ROUTES_BASE}/project/curate/exceptions", service)
        rendered = str(page)
        assert "0 failed executions · Active sweeps — not yet curated" in rendered
        assert "sel-sweep" not in rendered
        assert "Mark invalid" in rendered  # the bulkbar still mounts

    def test_mark_invalid_persists_reason_and_refreshes_the_rollup(
        self, mutable_client
    ):
        store, client = mutable_client
        callback_map = self._callback_map(client)
        response = self._triage_dispatch(
            client, callback_map, [str(SWEEP_A)], "bad shards"
        )
        row = store._curation_row(str(SWEEP_A))
        assert row[1] is not None and row[2] == "bad shards"
        assert "Marked invalid alpha" in str(response["exc-note"]["children"])
        assert response["exc-selection-count"]["children"] == "0 sweeps selected"
        # Alpha is incomplete, so it stays in the roll-up carrying its
        # curation badges; a terminal sweep would leave Active entirely.
        assert "badge invalid" in str(response["exc-groupsets"]["children"])

    def test_mark_invalid_requires_a_reason(self, mutable_client):
        store, client = mutable_client
        callback_map = self._callback_map(client)
        response = self._triage_dispatch(client, callback_map, [str(SWEEP_A)], "   ")
        assert "requires a reason" in str(response["exc-note"]["children"])
        assert store._curation_row(str(SWEEP_A))[1] is None
        assert "boom: divide by zero" in str(response["exc-groupsets"]["children"])

    def test_batch_mark_invalid_covers_every_checked_sweep(self, mutable_client):
        """The shared-bug case: one click invalidates every checked sweep
        through a single apply_curation call carrying the full id list."""
        store, client = mutable_client
        self._fail_sweep_b(store)
        callback_map = self._callback_map(client)
        response = self._triage_dispatch(
            client, callback_map, [str(SWEEP_A), str(SWEEP_B)], "bad shards"
        )
        for sweep_id in (SWEEP_A, SWEEP_B):
            row = store._curation_row(str(sweep_id))
            assert row[1] is not None and row[2] == "bad shards"
        assert "Marked invalid alpha, beta." in str(response["exc-note"]["children"])

    def test_empty_selection_prompts_without_acting(self, mutable_client):
        store, client = mutable_client
        self._fail_sweep_b(store)
        callback_map = self._callback_map(client)
        response = self._triage_dispatch(client, callback_map, [], "bad shards")
        assert "Select sweeps first" in str(response["exc-note"]["children"])
        assert "exc-groupsets" not in str(response)
        assert store._curation_row(str(SWEEP_A))[1] is None

    def test_mark_invalid_press_is_wired_to_the_selection_store(self, mutable_client):
        _store, client = mutable_client
        callback_map = self._callback_map(client)
        key = self._callback_key(callback_map, {"exc-selection-store.data"})
        spec = callback_map[key]
        assert [(dep["id"], dep["property"]) for dep in spec["inputs"]] == [
            ("exc-mark-invalid", "n_clicks")
        ]
        assert [(dep["id"], dep["property"]) for dep in spec["state"]] == [
            ("exc-reason", "value")
        ]
