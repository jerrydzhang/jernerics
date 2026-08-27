"""Cross-sweep analysis views (jernerics-h5d.13).

Callback-layer coverage over a heterogeneous three-sweep seed: token
round-trips, data catalog, series overlay, points tables, study-style
figures, URL hydration, and the continue-in-Python handoff. The
orchestrator browser-drives the mounted dashboard after merge, so these
tests assert on the pure helpers the Dash callbacks wrap plus
TestClient page 200s.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote

import pytest
from dash import dcc, no_update
from dash_ag_grid import AgGrid
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionEndEvent,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    Selection,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
)
from jernerics_server.dashboard.analysis import (
    EMPTY_TRAY,
    VIEW_VERSION,
    ViewStateError,
    analysis_href,
    analysis_page,
    catalog_tab,
    cold_start,
    control_values,
    decode_view_state,
    default_view_state,
    edited_fields,
    encode_view_state,
    expand_values,
    hydrate_tray,
    hydrate_view,
    mounted_selection,
    optuna_tab_content,
    points_tab,
    python_snippet,
    python_tab,
    scope_bar,
    search_from_state,
    search_from_tray,
    series_entry_href,
    series_outputs,
    sweep_picker_rows,
    synced_search,
    tray_from_edit,
    tray_summary,
    view_from_controls,
)
from jernerics_server.dashboard.app import build_dash_app
from jernerics_server.dashboard.auth import DashboardContext
from jernerics_server.dashboard.callbacks import page_content, tray_from_grid
from jernerics_server.dashboard.layout import shell
from jernerics_server.dashboard.selection_tokens import (
    SelectionTokenError,
    decode_selection_token,
    encode_selection_token,
)
from jernerics_server.dashboard.service import DashboardService
from jernerics_server.dashboard.sessions import SessionSigner
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store

API_KEY = "secret123"
PROJECT = "lab"

SWEEP_A = uuid.UUID("aa310000-0000-4000-8000-000000000000")
SWEEP_B = uuid.UUID("aa320000-0000-4000-8000-000000000000")
SWEEP_C = uuid.UUID("aa330000-0000-4000-8000-000000000000")
RA0 = uuid.UUID("cc310000-0000-4000-8000-000000000000")
RA1 = uuid.UUID("cc310100-0000-4000-8000-000000000000")
RA2 = uuid.UUID("cc310200-0000-4000-8000-000000000000")
TA = uuid.UUID("cc310300-0000-4000-8000-000000000000")
TB = uuid.UUID("cc320000-0000-4000-8000-000000000000")
TC = uuid.UUID("cc330000-0000-4000-8000-000000000000")
EXA0 = uuid.UUID("dd310000-0000-4000-8000-000000000000")
EXA1 = uuid.UUID("dd310100-0000-4000-8000-000000000000")
EXA2 = uuid.UUID("dd310200-0000-4000-8000-000000000000")
EXA3 = uuid.UUID("dd310300-0000-4000-8000-000000000000")
EXB = uuid.UUID("dd320000-0000-4000-8000-000000000000")
EXC = uuid.UUID("dd330000-0000-4000-8000-000000000000")

CLIENT_FORMAT_TOKEN = (
    "eyJzZWxlY3Rpb24iOnsiZXhlY3V0aW9ucyI6bnVsbCwicHJvamVjdCI6ImxhYiIsInJl"
    "dHJ5X3Jvb3RzIjpudWxsLCJzd2VlcHMiOlsiYWEzMTAwMDAtMDAwMC00MDAwLTgwMDAt"
    "MDAwMDAwMDAwMDAwIl0sInRyaWFscyI6bnVsbH0sInYiOjF9"
)
"""encode_selection(Selection(project="lab", sweeps=(SWEEP_A,))) — produced
by the jernerics client (h5d.9) and pasted here as a wire-format fixture."""

CROSS_PROJECT_TOKEN = (
    "eyJzZWxlY3Rpb24iOnsiZXhlY3V0aW9ucyI6bnVsbCwicHJvamVjdCI6Im90aGVyIiwicmV0cn"
    "lfcm9vdHMiOm51bGwsInN3ZWVwcyI6bnVsbCwidHJpYWxzIjpbImNjMzEwMjAwLTAwMDAtNDAw"
    "MC04MDAwLTAwMDAwMDAwMDAwMCJdfSwidiI6MX0"
)
"""encode_selection(Selection(project="other", trials=(RA2,))) — a token
minted for a different project."""


def _seed_events() -> list:
    """Heterogeneous project: sweep A (params lr/seed, stepped loss with
    context host+shard, a three-generation retry family whose final trial
    ran TWO executions), sweep B (lr only; loss+accuracy per step and a
    JSON summary; no context), sweep C (no params; one non-step scalar),
    plus a small artifact key set."""
    now = datetime.now(UTC)

    def at(seconds_ago: float) -> datetime:
        return now - timedelta(seconds=seconds_ago)

    def event(cls, seconds_ago: float, **kwargs):
        return cls(event_id=uuid.uuid4(), recorded_at=at(seconds_ago), **kwargs)

    return [
        event(
            SweepSnapshotEvent,
            1000,
            project=PROJECT,
            sweep_id=SWEEP_A,
            name="alpha",
            state="completed",
        ),
        event(
            SweepSnapshotEvent,
            900,
            project=PROJECT,
            sweep_id=SWEEP_B,
            name="beta",
            state="completed",
        ),
        event(
            SweepSnapshotEvent,
            800,
            project=PROJECT,
            sweep_id=SWEEP_C,
            name="gamma",
            state="completed",
        ),
        event(
            TrialSnapshotEvent,
            990,
            trial_id=RA0,
            sweep_id=SWEEP_A,
            number=1,
            state=TrialState.FAILED,
            retry_root_trial_id=RA0,
        ),
        event(
            TrialSnapshotEvent,
            970,
            trial_id=RA1,
            sweep_id=SWEEP_A,
            number=2,
            state=TrialState.FAILED,
            retry_of_trial_id=RA0,
            retry_root_trial_id=RA0,
            retry_index=1,
        ),
        event(
            TrialSnapshotEvent,
            950,
            trial_id=RA2,
            sweep_id=SWEEP_A,
            number=3,
            state=TrialState.COMPLETED,
            retry_of_trial_id=RA1,
            retry_root_trial_id=RA0,
            retry_index=2,
            objective=0.12,
            params=FlatContext({"lr": 0.1, "seed": 7}),
        ),
        event(
            TrialSnapshotEvent,
            940,
            trial_id=TA,
            sweep_id=SWEEP_A,
            number=4,
            state=TrialState.COMPLETED,
            retry_root_trial_id=TA,
            objective=0.34,
            params=FlatContext({"lr": 0.05, "seed": 42}),
        ),
        event(
            TrialSnapshotEvent,
            890,
            trial_id=TB,
            sweep_id=SWEEP_B,
            number=1,
            state=TrialState.COMPLETED,
            retry_root_trial_id=TB,
            objective=0.56,
            params=FlatContext({"lr": 0.2}),
        ),
        event(
            TrialSnapshotEvent,
            790,
            trial_id=TC,
            sweep_id=SWEEP_C,
            number=1,
            state=TrialState.COMPLETED,
            retry_root_trial_id=TC,
            objective=0.78,
        ),
        # Retry family, generation 0: one execution, one loss point.
        event(
            ExecutionStartEvent,
            985,
            execution_id=EXA0,
            trial_id=RA0,
            hostname="node00",
            started_at=at(985),
        ),
        event(
            ValueEvent,
            980,
            trial_id=RA0,
            key="loss",
            step=0,
            value=0.9,
            context=FlatContext({"host": "node00", "shard": 0}),
        ),
        event(
            ExecutionEndEvent,
            975,
            execution_id=EXA0,
            ended_at=at(975),
            outcome="failure",
            exit_code=1,
            failure_kind="stale_heartbeat",
        ),
        # Final generation RA2: TWO executions of ONE trial, each with its
        # own loss series — both must appear distinctly in overlays.
        event(
            ExecutionStartEvent,
            960,
            execution_id=EXA1,
            trial_id=RA2,
            hostname="node01",
            started_at=at(960),
        ),
        event(
            ValueEvent,
            955,
            trial_id=RA2,
            key="loss",
            step=0,
            value=0.5,
            context=FlatContext({"host": "node01", "shard": 0}),
        ),
        event(
            ValueEvent,
            950,
            trial_id=RA2,
            key="loss",
            step=1,
            value=0.4,
            context=FlatContext({"host": "node01", "shard": 0}),
        ),
        event(
            ValueEvent,
            945,
            trial_id=RA2,
            key="loss",
            step=2,
            value=0.3,
            context=FlatContext({"host": "node01", "shard": 0}),
        ),
        event(
            ExecutionEndEvent,
            940,
            execution_id=EXA1,
            ended_at=at(940),
            outcome="failure",
            exit_code=1,
            failure_kind="exception",
        ),
        event(
            ExecutionStartEvent,
            935,
            execution_id=EXA2,
            trial_id=RA2,
            hostname="node01",
            started_at=at(935),
        ),
        event(
            ValueEvent,
            930,
            trial_id=RA2,
            key="loss",
            step=0,
            value=0.6,
            context=FlatContext({"host": "node01", "shard": 1}),
        ),
        event(
            ValueEvent,
            925,
            trial_id=RA2,
            key="loss",
            step=1,
            value=0.5,
            context=FlatContext({"host": "node01", "shard": 1}),
        ),
        event(
            ValueEvent,
            920,
            trial_id=RA2,
            key="loss",
            step=2,
            value=0.4,
            context=FlatContext({"host": "node01", "shard": 1}),
        ),
        event(
            ValueEvent,
            915,
            trial_id=RA2,
            key="loss",
            step=3,
            value=0.35,
            context=FlatContext({"host": "node01", "shard": 1}),
        ),
        event(
            ExecutionEndEvent,
            910,
            execution_id=EXA2,
            ended_at=at(910),
            outcome="success",
            exit_code=0,
        ),
        event(
            ExecutionStartEvent,
            905,
            execution_id=EXA3,
            trial_id=TA,
            hostname="node02",
            started_at=at(905),
        ),
        event(
            ValueEvent,
            900,
            trial_id=TA,
            key="loss",
            step=0,
            value=0.45,
            context=FlatContext({"host": "node02", "shard": 0}),
        ),
        event(
            ValueEvent,
            895,
            trial_id=TA,
            key="loss",
            step=1,
            value=0.38,
            context=FlatContext({"host": "node02", "shard": 0}),
        ),
        event(
            ExecutionEndEvent,
            890,
            execution_id=EXA3,
            ended_at=at(890),
            outcome="success",
            exit_code=0,
        ),
        event(
            ExecutionStartEvent,
            880,
            execution_id=EXB,
            trial_id=TB,
            hostname="node10",
            started_at=at(880),
        ),
        event(ValueEvent, 875, trial_id=TB, key="loss", step=0, value=0.7),
        event(ValueEvent, 870, trial_id=TB, key="loss", step=1, value=0.6),
        event(ValueEvent, 865, trial_id=TB, key="accuracy", step=0, value=0.81),
        event(ValueEvent, 860, trial_id=TB, key="accuracy", step=1, value=0.91),
        event(
            ValueEvent,
            855,
            trial_id=TB,
            key="summary",
            step=0,
            observation={"acc": 0.91, "epochs": 2, "notes": "beta run"},
        ),
        event(
            ExecutionEndEvent,
            850,
            execution_id=EXB,
            ended_at=at(850),
            outcome="success",
            exit_code=0,
        ),
        event(
            ExecutionStartEvent,
            785,
            execution_id=EXC,
            trial_id=TC,
            hostname="node20",
            started_at=at(785),
        ),
        event(ValueEvent, 780, trial_id=TC, key="score", step=0, value=0.78),
        event(
            ExecutionEndEvent,
            775,
            execution_id=EXC,
            ended_at=at(775),
            outcome="success",
            exit_code=0,
        ),
        event(
            ArtifactDeclarationEvent,
            908,
            artifact_id=uuid.UUID("ff310000-0000-4000-8000-000000000000"),
            trial_id=RA2,
            execution_id=EXA2,
            key="checkpoint",
            filename="ckpt.pt",
            content_type="application/octet-stream",
            size_bytes=1024,
        ),
        event(
            ArtifactDeclarationEvent,
            888,
            artifact_id=uuid.UUID("ff310100-0000-4000-8000-000000000000"),
            trial_id=TA,
            key="report",
            filename="report.html",
            content_type="text/html",
            size_bytes=2048,
        ),
        event(
            ArtifactDeclarationEvent,
            848,
            artifact_id=uuid.UUID("ff320000-0000-4000-8000-000000000000"),
            trial_id=TB,
            execution_id=EXB,
            key="checkpoint",
            filename="ckpt.pt",
            content_type="application/octet-stream",
            size_bytes=4096,
        ),
    ]


def _seed_batches() -> list[list]:
    """One batch per execution stream. Ingest applies a batch tier by
    tier (every execution_start before any value), so two interleaved
    executions of one trial must arrive in separate batches for the
    resolver to attach each series to its own execution."""
    setup: list = []
    streams: list[list] = []
    artifacts: list = []
    for event in _seed_events():
        if isinstance(event, ExecutionStartEvent):
            streams.append([event])
        elif isinstance(event, ArtifactDeclarationEvent):
            artifacts.append(event)
        elif streams:
            streams[-1].append(event)
        else:
            setup.append(event)
    return [setup, *streams, artifacts]


def _ingest(store: Store) -> None:
    ingest = IngestService(store)
    for batch in _seed_batches():
        result = ingest.apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=batch)
        )
        assert not result.conflicts


def _tray(**overrides: Any) -> dict:
    tray = {
        "sweeps": [str(SWEEP_A)],
        "trials": [],
        "families": [],
        "executions": [],
        "expand": False,
    }
    tray.update(overrides)
    return tray


def _edit_tray(
    sweep_rows: list,
    family_rows: list,
    expand: list,
    current: dict | None,
    **edited: bool,
) -> dict:
    """``tray_from_edit`` with every control authoritative unless an
    explicit ``*_edited`` flag overrides it."""
    return tray_from_edit(
        sweep_rows,
        family_rows,
        expand,
        current,
        sweep_edited=edited.get("sweep_edited", True),
        family_edited=edited.get("family_edited", True),
        expand_edited=edited.get("expand_edited", True),
    )


def _seeded_store(tmp_path) -> Store:
    store = Store(tmp_path / "analysis.sqlite")
    _ingest(store)
    return store


@pytest.fixture
def service(tmp_path) -> DashboardService:
    return DashboardService(QueryService(_seeded_store(tmp_path)))


@pytest.fixture
def authed(tmp_path) -> TestClient:
    client = TestClient(
        create_app(
            _seeded_store(tmp_path),
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


def _walk(node, predicate):
    """Collect every descendant component matching ``predicate``."""
    found = []
    if predicate(node):
        found.append(node)
    children = getattr(node, "children", None)
    if children is None:
        return found
    children = children if isinstance(children, list) else [children]
    for child in children:
        if hasattr(node, "children") or hasattr(node, "type"):
            found.extend(_walk(child, predicate))
    return found


def _graphs(page):
    return _walk(page, lambda node: isinstance(node, dcc.Graph))


def _grids(page):
    return _walk(page, lambda node: isinstance(node, AgGrid))


def _pres(page):
    return _walk(page, lambda node: type(node).__name__ == "Pre")


def _selected_trial_ids(service: DashboardService, selection: Selection) -> list[str]:
    return sorted(str(record.trial_id) for record in service.queries.lineage(selection))


class TestSelectionTokens:
    def test_dashboard_round_trip_is_stable(self):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A,), trials=(RA2, TA))
        token = encode_selection_token(selection)
        assert encode_selection_token(selection) == token
        assert decode_selection_token(token) == selection

    def test_client_format_token_parses_and_reencodes_identically(self):
        selection = decode_selection_token(CLIENT_FORMAT_TOKEN)
        assert selection == Selection(project=PROJECT, sweeps=(SWEEP_A,))
        assert encode_selection_token(selection) == CLIENT_FORMAT_TOKEN

    def test_malformed_token_is_an_error(self):
        with pytest.raises(SelectionTokenError, match="malformed"):
            decode_selection_token("definitely-not-a-token-!!!")

    def test_cross_project_token_surfaces_error_not_mixed_results(self, service):
        with pytest.raises(SelectionTokenError, match="project 'other'"):
            decode_selection_token(CROSS_PROJECT_TOKEN, project=PROJECT)
        tray, error = hydrate_tray(
            service=service,
            project=PROJECT,
            pathname="/dashboard/analysis",
            search=f"?sel={CROSS_PROJECT_TOKEN}",
            current=None,
        )
        assert tray is None
        assert error is not None and "project" in error


class TestUnifiedSelectionStore:
    def test_token_hydration_feeds_tray_and_summary(self, service):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A, SWEEP_B))
        token = encode_selection_token(selection)
        tray, error = hydrate_tray(
            service, PROJECT, "/dashboard/analysis", f"?sel={token}", None
        )
        assert error is None and tray is not None
        assert tray["sweeps"] == [str(SWEEP_A), str(SWEEP_B)]
        assert tray["project"] == PROJECT
        assert tray_summary(tray).startswith(f"{len(selection.sweeps or ())} sweep(s)")
        # The same token against the hydrated store is a no-op, so the
        # ?sel= write-back stays stable instead of rewriting forever.
        again, error = hydrate_tray(
            service, PROJECT, "/dashboard/analysis", f"?sel={token}", tray
        )
        assert again is None and error is None

    def test_workspace_and_analysis_edits_hit_one_store(self):
        store = _edit_tray(
            [{"sweep_id": str(SWEEP_A)}],
            [{"root": str(RA0)}],
            [],
            dict(EMPTY_TRAY, project=PROJECT),
        )
        assert store["sweeps"] == [str(SWEEP_A)]
        assert store["families"] == [str(RA0)]
        # Workspace sweep-grid edit: sweeps replaced, analysis picks kept.
        store = tray_from_grid([{"sweep_id": str(SWEEP_B)}], store)
        assert store["sweeps"] == [str(SWEEP_B)]
        assert store["families"] == [str(RA0)]
        # Analysis edit (grids carry the workspace's picks as selected
        # rows): families replaced, sweeps kept, project survives.
        store = _edit_tray([{"sweep_id": str(SWEEP_B)}], [], ["expand"], store)
        assert store["sweeps"] == [str(SWEEP_B)]
        assert store["families"] == []
        assert store["expand"] is True
        assert store["project"] == PROJECT

    def test_workspace_grid_rows_reflect_the_unified_store(self, service):
        token = encode_selection_token(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        store, error = hydrate_tray(
            service, PROJECT, "/dashboard/analysis", f"?sel={token}", None
        )
        assert error is None and store is not None
        page, _polls = page_content(
            "/dashboard/project/lab", service, selected_sweeps=store["sweeps"]
        )
        grid = _grids(page)[0]
        assert [row["sweep_id"] for row in grid.selectedRows] == [str(SWEEP_A)]


class TestCellTextSelection:
    """jernerics-eqn: all four analysis grids carry the copyability pair
    through the shared helper, keeping rowSelection where present."""

    def test_picker_grids_carry_the_pair_and_multi_row_selection(self):
        pickers = _grids(analysis_page())
        assert [grid.id for grid in pickers] == [
            "analysis-sweep-grid",
            "analysis-family-grid",
        ]
        for grid in pickers:
            assert grid.dashGridOptions == {
                "enableCellTextSelection": True,
                "ensureDomOrder": True,
                "pagination": False,
                "rowSelection": {"mode": "multiRow"},
            }

    def test_points_grids_carry_the_pair(self, service):
        page = points_tab(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
        )
        grids = _grids(page)
        assert len(grids) == 2
        for grid in grids:
            assert grid.dashGridOptions == {
                "enableCellTextSelection": True,
                "ensureDomOrder": True,
                "pagination": False,
            }


class TestTray:
    def test_family_pick_without_expansion_selects_current_trial(self, service):
        tray = _edit_tray([], [{"root": str(RA0)}], [], None)
        selection = service.analysis_selection(PROJECT, tray)
        assert selection.trials == (RA2,)
        assert selection.retry_roots is None

    def test_expansion_uses_lineage_for_every_generation(self, service):
        tray = _edit_tray([], [{"root": str(RA0)}], ["expand"], None)
        selection = service.analysis_selection(PROJECT, tray)
        assert set(selection.trials or ()) == {RA0, RA1, RA2}

    def test_grid_picks_keep_explicit_trials(self):
        tray = _edit_tray(
            [{"sweep_id": str(SWEEP_B)}],
            [{"root": str(RA0)}],
            [],
            _tray(trials=[str(TC)]),
        )
        assert tray["sweeps"] == [str(SWEEP_B)]
        assert tray["trials"] == [str(TC)]
        assert tray["families"] == [str(RA0)]

    def test_grid_event_only_touches_its_own_dimension(self):
        """jernerics-8c9: an edit event from one grid leaves the other
        controls' picks alone — the sibling grid may still hold a stale
        selection snapshot while AG Grid applies programmatic rows."""
        tray = _edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None)
        family_echo = _edit_tray([], [], [], tray, sweep_edited=False)
        assert family_echo == tray

        family_tray = _edit_tray([], [{"root": str(RA0)}], ["expand"], None)
        expand_off = _edit_tray(
            [],
            [],
            [],
            family_tray,
            sweep_edited=False,
            family_edited=False,
            expand_edited=True,
        )
        assert expand_off["expand"] is False
        assert expand_off["families"] == [str(RA0)]

    def test_real_uncheck_of_the_last_sweep_clears(self):
        tray = _edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None)
        unchecked = _edit_tray([], [], [], tray, family_edited=False)
        assert unchecked["sweeps"] == []

    def test_mounting_grids_are_not_pushed_an_empty_selection(self):
        """jernerics-8c9: the loaders' initial call skips empty selectedRows
        writes — that write echoes back as an edit against a tray
        hydration may have landed in between."""
        assert mounted_selection([], initial=True) is no_update
        assert mounted_selection([], initial=False) == []
        assert mounted_selection([{"root": str(RA0)}], initial=True) == [
            {"root": str(RA0)}
        ]


class TestDataCatalog:
    def test_value_keys_kinds_points_and_step_presence(self, service):
        entries = {
            entry["key"]: entry
            for entry in service.analysis_value_keys(
                PROJECT,
                _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
            )
        }
        assert entries["loss"]["kind"] == "scalar"
        assert entries["loss"]["steps"] is True
        assert entries["loss"]["points"] == 12
        assert entries["accuracy"]["steps"] is True
        assert entries["summary"]["kind"] == "json"
        assert entries["summary"]["steps"] is False
        assert entries["score"]["steps"] is False

    def test_context_dimensions_with_cardinality(self, service):
        dims = {
            entry["key"]: entry
            for entry in service.analysis_context_dims(
                PROJECT, _tray(sweeps=[str(SWEEP_A)])
            )
        }
        assert set(dims) == {"host", "shard"}
        assert dims["host"]["cardinality"] == 3
        assert dims["shard"]["cardinality"] == 2
        assert len(dims["host"]["samples"]) == 3

    def test_param_coverage_marks_missing_sweeps(self, service):
        coverage = service.analysis_param_coverage(
            PROJECT,
            _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
        )
        rows = {row["key"]: row for row in coverage["rows"]}
        lr = rows["lr"]["cells"]
        assert lr[str(SWEEP_A)]["trials"] == 2
        assert lr[str(SWEEP_B)]["trials"] == 1
        assert lr[str(SWEEP_C)] is None
        seed = rows["seed"]["cells"]
        assert seed[str(SWEEP_A)]["trials"] == 2
        assert seed[str(SWEEP_B)] is None
        assert seed[str(SWEEP_C)] is None

    def test_artifact_keys_discovered(self, service):
        keys = {
            entry["key"]: entry
            for entry in service.analysis_artifacts(
                PROJECT,
                _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
            )
        }
        assert keys["checkpoint"]["count"] == 2
        assert keys["report"]["count"] == 1

    def test_catalog_tab_renders_sections(self, service):
        page = catalog_tab(
            service, PROJECT, _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)])
        )
        rendered = str(page)
        for needle in (
            "loss",
            "summary",
            "host",
            "shard",
            "lr",
            "seed",
            "checkpoint",
        ):
            assert needle in rendered


class TestSeriesOverlay:
    def test_one_trace_per_trial_execution_pair(self, service):
        figure, *_rest = series_outputs(
            service, PROJECT, _tray(), "loss", None, None, "none"
        )
        names = {trace.name for trace in figure.data}
        assert names == {
            "cc310000/dd310000",
            "cc310200/dd310100",
            "cc310200/dd310200",
            "cc310300/dd310300",
        }
        points = {trace.name: len(trace.x) for trace in figure.data}
        assert points["cc310200/dd310100"] == 3
        assert points["cc310200/dd310200"] == 4

    def test_no_reduction_returns_every_point(self, service):
        figure, *_rest = series_outputs(
            service, PROJECT, _tray(), "loss", None, None, "none"
        )
        assert sum(len(trace.x) for trace in figure.data) == 10

    def test_mean_reduction_folds_executions_per_trial(self, service):
        figure, *_rest = series_outputs(
            service, PROJECT, _tray(), "loss", None, None, "mean"
        )
        names = {trace.name for trace in figure.data}
        assert names == {"cc310000", "cc310200", "cc310300"}
        merged = next(t for t in figure.data if t.name == "cc310200")
        assert list(merged.x) == [0, 1, 2, 3]
        assert merged.y[0] == pytest.approx((0.5 + 0.6) / 2)
        assert merged.y[3] == pytest.approx(0.35)

    def test_context_dimensions_offer_color_and_facet(self, service):
        all_sweeps = _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)])
        _figure, key_options, color_options, facet_options = series_outputs(
            service, PROJECT, all_sweeps, "loss", None, None, "none"
        )
        assert {"loss", "accuracy", "summary", "score"} <= {
            option["value"] for option in key_options
        }
        assert {option["value"] for option in color_options} == {"host", "shard"}
        assert facet_options == color_options

    def test_context_color_keys_traces_by_dimension(self, service):
        figure, *_rest = series_outputs(
            service, PROJECT, _tray(), "loss", "shard", None, "none"
        )
        assert len(figure.data) == 4
        by_name = {trace.name: trace for trace in figure.data}
        shard_zero = [
            by_name["cc310000/dd310000"],
            by_name["cc310200/dd310100"],
            by_name["cc310300/dd310300"],
        ]
        assert len({trace.line.color for trace in shard_zero}) == 1
        assert by_name["cc310200/dd310200"].line.color != shard_zero[0].line.color
        assert len({trace.line.color for trace in figure.data}) == 2


class TestPointsTable:
    def test_json_pretty_strings_and_missing_markers(self, service):
        page = points_tab(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
        )
        grids = _grids(page)
        values_grid, params_grid = grids[0], grids[1]
        value_rows = {row["trial"]: row for row in values_grid.rowData}
        tb_row = next(
            row for label, row in value_rows.items() if label.startswith("#1 cc32")
        )
        assert tb_row["summary"] == json.dumps(
            {"acc": 0.91, "epochs": 2, "notes": "beta run"},
            indent=2,
            sort_keys=True,
        )
        assert "\n" in tb_row["summary"]
        ra0_row = next(
            row for label, row in value_rows.items() if label.startswith("#1 cc31")
        )
        assert ra0_row["summary"] == "—"
        assert ra0_row["score"] == "—"
        param_rows = {row["trial"]: row for row in params_grid.rowData}
        tc_row = next(
            row for label, row in param_rows.items() if label.startswith("#1 cc33")
        )
        assert tc_row["lr"] == "—"
        assert tc_row["seed"] == "—"
        assert tc_row["lr"] != "0.2"

    def test_presence_counts_in_column_headers(self, service):
        page = points_tab(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
        )
        headers = [column["headerName"] for column in _grids(page)[1].columnDefs]
        assert any(header.startswith("lr · 3/6") for header in headers), headers
        assert any(header.startswith("seed · 2/6") for header in headers), headers


class TestOptunaFigures:
    def test_sweep_a_figure_set(self, service):
        content, x_options, y_options = optuna_tab_content(
            service, PROJECT, _tray(), None, None
        )
        graphs = [graph.figure for graph in _graphs(content)]
        history, parcoords, slice_fig, contour, timeline = graphs
        completed = [t for t in history.data[0].x]
        assert completed == [3, 4]
        assert history.data[0].y == (0.12, 0.34)
        labels = [dim["label"] for dim in parcoords.data[0].dimensions]
        assert labels == ["lr", "seed", "objective"]
        assert len(slice_fig.data) == 2
        assert contour.data[0].type == "contour"
        assert {option["value"] for option in x_options} == {"lr", "seed"}
        assert y_options == x_options
        assert timeline.data[0].type == "bar"
        assert len(timeline.data[0].y) == 4

    def test_param_less_sweep_degrades_to_empty_contour(self, service):
        content, x_options, _y = optuna_tab_content(
            service, PROJECT, _tray(sweeps=[str(SWEEP_C)]), None, None
        )
        graphs = [graph.figure for graph in _graphs(content)]
        assert len(graphs) == 4
        parcoords = graphs[1]
        labels = [dim["label"] for dim in parcoords.data[0].dimensions]
        assert labels == ["objective"]
        rendered = str(content)
        assert "at least two numeric" in rendered
        assert x_options == []


class TestUrlReload:
    def test_token_hydrates_equal_tray_selection(self, service):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A,), trials=(RA2,))
        search = f"?sel={encode_selection_token(selection)}"
        tray, error = hydrate_tray(
            service, PROJECT, "/dashboard/analysis", search, None
        )
        assert error is None and tray is not None
        assert service.analysis_selection(PROJECT, tray) == selection
        assert expand_values(tray) == []

    def test_retry_root_token_hydrates_with_expansion(self, service):
        selection = Selection(project=PROJECT, retry_roots=(RA0,))
        tray, error = hydrate_tray(
            service,
            PROJECT,
            "/dashboard/analysis",
            f"?sel={encode_selection_token(selection)}",
            None,
        )
        assert error is None and tray is not None
        assert expand_values(tray) == ["expand"]
        assert _selected_trial_ids(
            service, service.analysis_selection(PROJECT, tray)
        ) == _selected_trial_ids(service, selection)

    def test_search_round_trip_from_tray(self, service):
        tray = _edit_tray([], [{"root": str(RA0)}], ["expand"], None)
        search = search_from_tray(service, PROJECT, tray, "")
        assert search is not None and search.startswith("?sel=")
        hydrated, error = hydrate_tray(
            service, PROJECT, "/dashboard/analysis", search, None
        )
        assert error is None
        assert service.analysis_selection(PROJECT, hydrated) == (
            service.analysis_selection(PROJECT, tray)
        )
        assert search_from_tray(service, PROJECT, hydrated, search) is None

    def test_unchanged_search_is_not_rewritten(self, service):
        tray = _tray()
        search = search_from_tray(service, PROJECT, tray, "")
        assert search_from_tray(service, PROJECT, tray, search) is None

    def test_non_analysis_url_is_ignored(self, service):
        tray, error = hydrate_tray(
            service,
            PROJECT,
            "/dashboard/sweep/x",
            f"?sel={encode_selection_token(Selection(project=PROJECT))}",
            None,
        )
        assert tray is None and error is None


class TestColdStartAdoption:
    """jernerics-xbx: a shared ``?sel=`` token opened before any project
    is picked adopts the token's project through the picker — the same
    settle path as a manual pick (picker -> project-store -> hydration
    re-fires). Adoption offers only projects the dashboard has data
    for; anything else surfaces an error naming the project instead of
    silently empty grids."""

    def test_known_project_token_offers_the_adoption(self, service):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A, SWEEP_B))
        adopted, error = cold_start(
            service, f"?sel={encode_selection_token(selection)}"
        )
        assert error is None and adopted == selection

    def test_unknown_project_token_names_itself_in_the_hint(self, service):
        token = encode_selection_token(Selection(project="ghost", trials=(RA0,)))
        adopted, error = cold_start(service, f"?sel={token}")
        assert adopted is None
        assert error is not None and "project 'ghost'" in error

    def test_invalid_token_errors_and_no_token_stays_quiet(self, service):
        _adopted, error = cold_start(service, "?sel=definitely-not-a-token-!!!")
        assert error is not None and "malformed" in error
        assert cold_start(service, "") == (None, None)
        assert cold_start(service, None) == (None, None)

    def test_fresh_session_hydration_waits_for_the_settle(self, service):
        """No project picked: hydration leaves the tray alone; once the
        picker adoption settles project-store, the ordinary path
        hydrates, and its own settle re-fire is a no-op."""
        token = encode_selection_token(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        search = f"?sel={token}"
        tray, error = hydrate_tray(
            service, None, "/dashboard/analysis", search, dict(EMPTY_TRAY)
        )
        assert tray is None and error is None
        adopted, _error = cold_start(service, search)
        assert adopted is not None and adopted.project == PROJECT
        hydrated, error = hydrate_tray(
            service, adopted.project, "/dashboard/analysis", search, dict(EMPTY_TRAY)
        )
        assert error is None and hydrated is not None
        assert hydrated["sweeps"] == [str(SWEEP_A)]
        assert hydrate_tray(
            service, adopted.project, "/dashboard/analysis", search, hydrated
        ) == (None, None)

    def test_unknown_project_token_hints_on_the_analysis_page(self, service):
        token = encode_selection_token(Selection(project="ghost", trials=(RA0,)))
        tray, error = hydrate_tray(
            service, None, "/dashboard/analysis", f"?sel={token}", dict(EMPTY_TRAY)
        )
        assert tray is None
        assert error is not None and "project 'ghost'" in error

    def test_invalid_token_on_fresh_session_still_errors(self, service):
        tray, error = hydrate_tray(
            service,
            None,
            "/dashboard/analysis",
            "?sel=definitely-not-a-token-!!!",
            dict(EMPTY_TRAY),
        )
        assert tray is None
        assert error is not None and "malformed" in error

    def test_off_analysis_url_neither_adopts_nor_errors(self, service):
        token = encode_selection_token(Selection(project=PROJECT))
        assert hydrate_tray(
            service, None, "/dashboard/", f"?sel={token}", dict(EMPTY_TRAY)
        ) == (None, None)


class TestUrlSync:
    """jernerics-8c9: one shell-only callback owns ``url.search`` — tray
    edits mint ``?sel=`` on the analysis page, navigations strip it, and
    a navigation never mints (a stale session tray would clobber a
    freshly opened deep link before hydration lands)."""

    def test_tray_edit_on_analysis_mints_the_token(self, service):
        tray = _edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None)
        target = synced_search(
            service, "/dashboard/analysis", tray, "", PROJECT, url_navigated=False
        )
        assert target is not None and target.startswith("?sel=")
        assert decode_selection_token(target.removeprefix("?sel=")) == (
            service.analysis_selection(PROJECT, tray)
        )

    def test_unchanged_tray_edit_leaves_the_search_alone(self, service):
        tray = _tray()
        search = search_from_tray(service, PROJECT, tray, "")
        assert (
            synced_search(
                service,
                "/dashboard/analysis",
                tray,
                search,
                PROJECT,
                url_navigated=False,
            )
            is None
        )

    def test_tray_edit_off_analysis_leaves_the_search_alone(self, service):
        tray = _edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None)
        assert (
            synced_search(
                service,
                "/dashboard/project/lab",
                tray,
                "?sel=tok",
                PROJECT,
                url_navigated=False,
            )
            is None
        )

    def test_navigation_off_analysis_strips_the_token(self, service):
        assert (
            synced_search(
                service, "/dashboard/", None, "?sel=tok", PROJECT, url_navigated=True
            )
            == ""
        )

    def test_navigation_with_no_search_leaves_the_url_alone(self, service):
        assert (
            synced_search(
                service, "/dashboard/sweep/x", None, "", None, url_navigated=True
            )
            is None
        )

    def test_navigation_onto_analysis_never_mints_over_a_deep_link(self, service):
        session_tray = _edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None)
        deep_link = "?sel=" + encode_selection_token(
            Selection(project=PROJECT, sweeps=(SWEEP_B,))
        )
        assert (
            synced_search(
                service,
                "/dashboard/analysis",
                session_tray,
                deep_link,
                PROJECT,
                url_navigated=True,
            )
            is None
        )

    def test_pre_picked_deep_link_survives_the_mount_echo(self, service):
        """The 8c9 regression ordering, as one callback sequence: with a
        session project already settled, a deep link hydrates while the
        picker grids are still mounting; AG Grid's stale mount echo then
        reports empty picks. The tray survives and ?sel= is neither
        stripped nor rewritten."""
        session_tray = {**EMPTY_TRAY, "project": PROJECT}
        search = "?sel=" + encode_selection_token(
            Selection(project=PROJECT, sweeps=(SWEEP_A,))
        )
        hydrated, error = hydrate_tray(
            service, PROJECT, "/dashboard/analysis", search, session_tray
        )
        assert error is None and hydrated is not None
        # loaders' initial call: empty selection is not pushed to the
        # mounting grids, so no empty-selection echo can fire
        assert mounted_selection([], initial=True) is no_update
        # the stale echo (family grid mounted, sweep grid not yet applied)
        echo = tray_from_edit(
            [],
            [],
            [],
            hydrated,
            sweep_edited=False,
            family_edited=True,
            expand_edited=False,
        )
        assert echo == hydrated  # equality guard prevents the edit
        assert (
            synced_search(
                service,
                "/dashboard/analysis",
                echo,
                search,
                PROJECT,
                url_navigated=False,
            )
            is None
        )

    def test_cold_start_deep_link_settles_through_adoption(self, service):
        """jernerics-xbx ordering, as one callback sequence: the picker
        adopts the token's project, project-store settles through the
        picker's own remember callback (the manual-pick path), hydration
        re-fires with a project and the tray lands — the clear guard
        sees a matching project, the grid echo edits nothing, and ?sel=
        survives untouched."""
        search = "?sel=" + encode_selection_token(
            Selection(project=PROJECT, sweeps=(SWEEP_A,))
        )
        adoption, error = cold_start(service, search)
        assert error is None and adoption is not None
        assert adoption.project == PROJECT
        # project-store settled: hydration re-fires with the project
        hydrated, error = hydrate_tray(
            service, adoption.project, "/dashboard/analysis", search, dict(EMPTY_TRAY)
        )
        assert error is None and hydrated is not None
        # _clear_selection_on_project_change: the hydrated tray already
        # carries the settled project — nothing to wipe
        assert hydrated["project"] == adoption.project
        # the settle re-fire against the hydrated tray rewrites nothing
        assert hydrate_tray(
            service, PROJECT, "/dashboard/analysis", search, hydrated
        ) == (None, None)
        # loaders push the hydrated selection to the now-populated grids
        _rows, selected = sweep_picker_rows(service.sweep_overview(PROJECT), hydrated)
        assert [row["sweep_id"] for row in selected] == [str(SWEEP_A)]
        # a grid echo against the settled tray edits nothing
        echo = tray_from_edit(
            [],
            [],
            [],
            hydrated,
            sweep_edited=False,
            family_edited=True,
            expand_edited=False,
        )
        assert echo == hydrated
        assert (
            synced_search(
                service,
                "/dashboard/analysis",
                echo,
                search,
                PROJECT,
                url_navigated=False,
            )
            is None
        )


class TestViewStateCodec:
    """jernerics-cdf.3: the dashboard-only ``view=`` document — version,
    enums, JSON types, unknown-field dropping, defaults for missing
    fields, and percent-encoded compact JSON round-trips."""

    def test_round_trip_is_exact_and_url_safe(self):
        doc = default_view_state()
        doc["active"] = "series"
        doc["series"] = {
            **doc["series"],
            "keys": ["loss", "accuracy"],
            "reduction": "mean",
            "trial_display": "short",
            "context_filters": {"host": ["node00", "node01"]},
            "color": "shard",
            "axes": {"loss": "y1", "accuracy": "y2"},
        }
        doc["highlighted_families"] = [str(RA0)]
        doc["auto_refresh"] = True
        doc["optuna"] = {"contour_x": "lr", "contour_y": "seed"}
        encoded = encode_view_state(doc)
        assert all(char not in '{}":,&' for char in encoded)
        assert decode_view_state(unquote(encoded)) == doc
        assert encode_view_state(decode_view_state(unquote(encoded))) == encoded

    def test_unknown_fields_are_dropped_and_not_re_emitted(self):
        doc = default_view_state()
        doc["active"] = "points"
        payload = json.dumps(
            {
                **doc,
                "layout": "saved",
                "series": {**doc["series"], "hover": 1, "plotly": {"x": 2}},
                "optuna": {**doc["optuna"], "zoom": "in"},
            }
        )
        assert decode_view_state(payload) == doc

    def test_missing_fields_take_defaults(self):
        assert decode_view_state(json.dumps({"v": VIEW_VERSION})) == (
            default_view_state()
        )
        partial = json.dumps({"v": VIEW_VERSION, "active": "optuna"})
        assert decode_view_state(partial)["active"] == "optuna"
        assert decode_view_state(partial)["series"]["keys"] == []

    @pytest.mark.parametrize(
        "payload",
        [
            "not json",
            "[1, 2]",
            json.dumps({"v": 2}),
            json.dumps({"v": True}),
            json.dumps({"v": VIEW_VERSION, "active": "selection"}),
            json.dumps({"v": VIEW_VERSION, "active": "catalog", "series": []}),
            json.dumps({"v": VIEW_VERSION, "series": {"keys": "loss"}}),
            json.dumps({"v": VIEW_VERSION, "series": {"keys": [""]}}),
            json.dumps({"v": VIEW_VERSION, "series": {"mode": "scatter"}}),
            json.dumps({"v": VIEW_VERSION, "series": {"reduction": "latest"}}),
            json.dumps({"v": VIEW_VERSION, "series": {"color": 3}}),
            json.dumps({"v": VIEW_VERSION, "series": {"context_filters": []}}),
            json.dumps(
                {"v": VIEW_VERSION, "series": {"context_filters": {"host": "n0"}}}
            ),
            json.dumps({"v": VIEW_VERSION, "series": {"axes": {"loss": 1}}}),
            json.dumps({"v": VIEW_VERSION, "highlighted_families": [7]}),
            json.dumps({"v": VIEW_VERSION, "auto_refresh": "yes"}),
            json.dumps({"v": VIEW_VERSION, "optuna": {"contour_x": 4}}),
        ],
    )
    def test_bad_payloads_fail_with_descriptive_errors(self, payload):
        with pytest.raises(ViewStateError):
            decode_view_state(payload)

    def test_error_messages_name_the_problem(self):
        with pytest.raises(ViewStateError, match="expected version 1"):
            decode_view_state(json.dumps({"v": 2}))
        with pytest.raises(ViewStateError, match="unsupported analysis view"):
            decode_view_state(json.dumps({"v": 1, "active": "selection"}))
        with pytest.raises(ViewStateError, match="malformed"):
            decode_view_state("%zz{")

    def test_duplicate_keys_keep_first_occurrence_order(self):
        doc = default_view_state()
        doc["series"]["keys"] = ["loss", "accuracy", "loss"]
        decoded = decode_view_state(json.dumps(doc))
        assert decoded["series"]["keys"] == ["loss", "accuracy"]


class TestViewHydration:
    def test_valid_document_lands_in_the_store(self):
        doc = dict(default_view_state(), active="series")
        hydrated, error = hydrate_view(
            "/dashboard/analysis", f"?view={encode_view_state(doc)}", None
        )
        assert error is None and hydrated == doc

    def test_equal_state_is_left_alone(self):
        doc = dict(default_view_state(), active="series")
        search = f"?view={encode_view_state(doc)}"
        assert hydrate_view("/dashboard/analysis", search, doc) == (None, None)

    def test_no_parameter_means_defaults(self):
        assert hydrate_view("/dashboard/analysis", "?sel=tok", None) == (
            default_view_state(),
            None,
        )
        doc = dict(default_view_state(), active="points")
        assert hydrate_view("/dashboard/analysis", "", doc) == (
            default_view_state(),
            None,
        )

    def test_malformed_document_defaults_with_visible_error(self):
        hydrated, error = hydrate_view(
            "/dashboard/analysis", "?view=%7Bbroken", dict(default_view_state())
        )
        assert hydrated == default_view_state()
        assert error is not None and "view state" in error

    def test_off_analysis_route_the_store_is_untouched(self):
        doc = dict(default_view_state(), active="points")
        assert hydrate_view("/dashboard/sweep/x", "?view=%7Bbroken", doc) == (
            None,
            None,
        )


class TestViewSync:
    """jernerics-cdf.3: one shell callback owns ``url.search`` and mints
    ``?sel=`` + ``?view=`` together; defaults stay out of the URL, and
    undecodable garbage is never silently rewritten away."""

    def test_view_edit_mints_both_parameters(self, service):
        tray = _tray()
        doc = dict(default_view_state(), active="series")
        target = search_from_state(service, PROJECT, tray, doc, "")
        assert target is not None and target.startswith("?sel=")
        assert "&view=" in target
        raw = target.split("&view=")[1]
        assert decode_view_state(unquote(raw)) == doc

    def test_default_view_state_is_not_emitted(self, service):
        target = search_from_state(service, PROJECT, _tray(), default_view_state(), "")
        assert target == search_from_tray(service, PROJECT, _tray(), "")
        assert target is None or "view=" not in target

    def test_view_only_state_mints_without_sel(self, service):
        doc = dict(default_view_state(), active="points")
        target = search_from_state(service, PROJECT, None, doc, "")
        assert target is not None and target.startswith("?view=")

    def test_unchanged_state_is_not_rewritten(self, service):
        tray = _tray()
        doc = dict(default_view_state(), active="series")
        target = search_from_state(service, PROJECT, tray, doc, "")
        assert target is not None
        assert search_from_state(service, PROJECT, tray, doc, target) is None

    def test_undecodable_view_parameter_is_left_in_place(self, service):
        tray = _tray()
        sel = encode_selection_token(service.analysis_selection(PROJECT, tray))
        current = "?sel=" + sel + "&view=%zz{"
        assert (
            search_from_state(service, PROJECT, tray, default_view_state(), current)
            is None
        )

    _ALL_FIELDS = {
        "active",
        "key",
        "reduction",
        "color",
        "facet",
        "contour_x",
        "contour_y",
    }

    def test_control_edits_rebuild_state_and_preserve_uncontrolled_fields(self):
        doc = default_view_state()
        doc["series"] = {
            **doc["series"],
            "keys": ["loss", "accuracy"],
            "axes": {"loss": "y1"},
            "context_filters": {"host": ["node00"]},
        }
        doc["auto_refresh"] = True
        edited = view_from_controls(
            doc,
            active="series",
            key="accuracy",
            reduction="max",
            color="shard",
            facet=None,
            contour_x="lr",
            contour_y="seed",
            edited=self._ALL_FIELDS,
        )
        assert edited["active"] == "series"
        assert edited["series"]["keys"] == ["accuracy"]
        assert edited["series"]["reduction"] == "max"
        assert edited["series"]["axes"] == {"loss": "y1"}
        assert edited["series"]["context_filters"] == {"host": ["node00"]}
        assert edited["auto_refresh"] is True

    def test_untriggered_controls_never_read_as_clears(self):
        """The control-sync write fires the edit callback with every
        input; a dropdown whose options have not loaded reports None and
        must not wipe the hydrated key."""
        doc = dict(default_view_state(), active="series")
        doc["series"] = {
            **doc["series"],
            "keys": ["loss"],
            "color": "shard",
            "reduction": "mean",
        }
        tab_echo = view_from_controls(
            doc,
            active="series",
            key=None,
            reduction="mean",
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            edited={"active", "reduction"},
        )
        assert tab_echo["series"]["keys"] == ["loss"]
        assert tab_echo["series"]["color"] == "shard"
        assert tab_echo == doc

    def test_edited_fields_maps_triggered_prop_ids(self):
        assert edited_fields({"analysis-tabs.value", "analysis-contour-x.value"}) == {
            "active",
            "contour_x",
        }
        assert edited_fields({"url.search"}) == set()

    _LOADED = {
        "key": {"loss", "accuracy", "summary"},
        "color": {"host", "shard"},
        "facet": {"host", "shard"},
        "contour_x": {"lr", "seed"},
        "contour_y": {"lr", "seed"},
    }

    def test_control_values_read_the_first_series_key(self):
        doc = default_view_state()
        doc["series"]["keys"] = ["loss", "accuracy"]
        active, key, reduction, color, facet, cx, cy = control_values(doc, self._LOADED)
        assert (active, key, reduction) == ("catalog", "loss", "none")
        assert (color, facet, cx, cy) == (None, None, None, None)
        assert control_values(None, self._LOADED)[1] is None

    def test_values_wait_for_their_options(self):
        """A value written before its options exist is dropped by the
        dropdown and fires back as a spurious clear — gating keeps it
        for the options-arrival write."""
        doc = default_view_state()
        doc["series"] = {**doc["series"], "keys": ["loss"], "color": "shard"}
        doc["optuna"] = {**doc["optuna"], "contour_x": "lr"}
        unloaded = {name: None for name in self._LOADED}
        _active, key, _reduction, color, _facet, cx, _cy = control_values(doc, unloaded)
        assert key is no_update
        assert color is no_update
        assert cx is no_update
        partial = {**self._LOADED, "key": set()}
        _active, key, _reduction, _color, _facet, cx, _cy = control_values(doc, partial)
        assert key is no_update
        assert cx == "lr"


class TestScopeBar:
    def test_shows_sweep_names_and_counts(self, service):
        bar = scope_bar(service, PROJECT, _tray(sweeps=[str(SWEEP_A), str(SWEEP_B)]))
        rendered = str(bar)
        assert "Scope: alpha, beta" in rendered
        assert "2 sweep(s)" in rendered
        assert "0 family/families" in rendered

    def test_unknown_sweep_id_falls_back_to_short_id(self, service):
        bar = scope_bar(service, PROJECT, _tray(sweeps=[str(SWEEP_C)]))
        assert "Scope: gamma" in str(bar)

    def test_expansion_and_executions_surface_in_counts(self, service):
        tray = _tray(families=[str(RA0)], executions=[str(EXA1)], expand=True)
        rendered = str(scope_bar(service, PROJECT, tray))
        assert "1 family/families" in rendered
        assert "1 execution(s)" in rendered
        assert "retry families expanded" in rendered

    def test_projectless_bar_tells_the_user_to_pick(self):
        rendered = str(scope_bar(None, None, None))
        assert "Pick a project" in rendered

    def test_scope_bar_sits_above_tabs_and_selection_tab_is_gone(self):
        page = analysis_page()
        rendered = str(page)
        assert "Edit scope" in rendered
        assert "analysis-scope-bar" in rendered
        assert "analysis-sweep-grid" in rendered
        assert "analysis-family-grid" in rendered
        assert rendered.index("analysis-scope-bar") < rendered.index("analysis-tabs")
        tabs = next(
            node
            for node in _walk(page, lambda n: type(n).__name__ == "Tabs")
            if node.id == "analysis-tabs"
        )
        assert [tab.value for tab in tabs.children] == [
            "catalog",
            "series",
            "points",
            "optuna",
            "python",
        ]


class TestEntryPoints:
    """Direct doors into Analysis: sweep detail's Analyze series and the
    header tray."""

    def test_series_entry_scopes_to_exactly_that_sweep_with_series_active(self):
        href = series_entry_href(PROJECT, str(SWEEP_A))
        assert href.startswith("/dashboard/analysis?")
        sel, view = href.removeprefix("/dashboard/analysis?").split("&view=")
        assert sel.startswith("sel=")
        assert decode_selection_token(sel.removeprefix("sel=")) == Selection(
            project=PROJECT, sweeps=(SWEEP_A,)
        )
        assert decode_view_state(unquote(view))["active"] == "series"

    def test_series_entry_needs_no_workspace_selection(self):
        href = series_entry_href(PROJECT, str(SWEEP_B))
        assert "sweep-grid" not in href
        selection = decode_selection_token(href.split("sel=")[1].split("&")[0])
        assert selection.sweeps == (SWEEP_B,)
        assert selection.trials is None

    def test_tray_href_carries_the_current_scope(self, service):
        href = analysis_href(service, PROJECT, _tray())
        assert href.startswith("/dashboard/analysis?sel=")
        token = href.split("sel=")[1]
        assert decode_selection_token(token) == (
            service.analysis_selection(PROJECT, _tray())
        )

    def test_empty_tray_href_is_a_plain_analysis_link(self, service):
        assert analysis_href(service, PROJECT, dict(EMPTY_TRAY)) == (
            "/dashboard/analysis"
        )

    def test_shell_tray_is_a_link_and_view_store_starts_at_defaults(self):
        anchor = next(
            node
            for node in _walk(
                shell(), lambda n: getattr(n, "id", None) == "selection-tray"
            )
        )
        assert type(anchor).__name__ == "A"
        store = next(
            node
            for node in _walk(shell(), lambda n: getattr(n, "id", None) == "view-store")
        )
        assert store.data == default_view_state()


class TestCallbackGraphSafety:
    """jernerics-8c9 regression: a callback firable on any page — one
    with an input id mounted in the shell — must reference only
    shell-mounted ids in its states and may not mix shell and page-local
    outputs. Dash raises ReferenceError for exactly that shape when the
    page-local component is unmounted (fully page-local outputs are
    pruned silently, so they are allowed)."""

    @pytest.fixture(scope="class")
    def callback_map(self, tmp_path_factory):
        service = DashboardService(
            QueryService(_seeded_store(tmp_path_factory.mktemp("callback-graph")))
        )
        ctx = DashboardContext(
            api_key="secret123",
            queries=service.queries,
            service=service,
            signer=SessionSigner(b"\x00" * 32),
        )
        return build_dash_app(ctx).callback_map

    @staticmethod
    def _outputs(key: str) -> set[str]:
        """The ``id.property`` output specs behind a callback_map key
        (``...``-joined multi-outputs, ``@<hash>`` duplicate suffixes)."""
        stripped = key.removeprefix("..").removesuffix("..")
        return {part.split("@")[0] for part in stripped.split("...") if part}

    def test_url_search_has_exactly_one_owner(self, callback_map):
        owners = [key for key in callback_map if self._outputs(key) == {"url.search"}]
        assert owners == ["url.search"]

    def test_everywhere_firable_callbacks_never_mix_shell_and_page_ids(
        self, callback_map
    ):
        shell_ids = {
            node.id
            for node in _walk(
                shell(), lambda node: isinstance(getattr(node, "id", None), str)
            )
        }
        for key, spec in callback_map.items():
            inputs = {dep["id"] for dep in spec["inputs"]}
            if not inputs & shell_ids:
                continue
            output_ids = {spec.rsplit(".", 1)[0] for spec in self._outputs(key)}
            assert not (output_ids & shell_ids and output_ids - shell_ids), (
                key,
                sorted(output_ids),
            )
            states = {dep["id"] for dep in spec.get("state", [])}
            assert states <= shell_ids, key

    def test_analysis_grids_write_the_shell_selection_store(self, callback_map):
        grid_writers = [
            key
            for key, spec in callback_map.items()
            if {"analysis-sweep-grid", "analysis-family-grid"}
            & {dep["id"] for dep in spec["inputs"]}
        ]
        assert len(grid_writers) == 1
        assert self._outputs(grid_writers[0]) == {"selection-store.data"}


class TestColdStartMountedJourney:
    """jernerics-xbx over the mounted app: the registered callbacks,
    driven through Dash's own dispatch endpoint exactly as the browser
    would on a fresh deep link — the picker adopts the token's project,
    the remember callback settles project-store, hydration re-fires and
    the tray lands."""

    @pytest.fixture(scope="class")
    def callback_map(self, tmp_path_factory):
        service = DashboardService(
            QueryService(_seeded_store(tmp_path_factory.mktemp("cold-start")))
        )
        ctx = DashboardContext(
            api_key="secret123",
            queries=service.queries,
            service=service,
            signer=SessionSigner(b"\x00" * 32),
        )
        return build_dash_app(ctx).callback_map

    @staticmethod
    def _callback_key(callback_map, wanted: set[str]) -> str:
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

    _HYDRATION_OUTPUTS = {
        "selection-store.data",
        "analysis-message-store.data",
        "view-store.data",
    }

    def test_cold_start_settles_picker_store_then_tray(self, authed, callback_map):
        token = encode_selection_token(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        search = f"?sel={token}"
        # the picker callback adopts the token's project
        picker = self._dispatch(
            authed,
            callback_map,
            {"project-picker.value"},
            [
                {"id": "project-store", "property": "data", "value": None},
                {"id": "url", "property": "search", "value": search},
            ],
        )
        assert picker["project-picker"]["value"] == PROJECT
        # the remember callback settles project-store (the manual-pick path)
        store = self._dispatch(
            authed,
            callback_map,
            {"project-store.data"},
            [{"id": "project-picker", "property": "value", "value": PROJECT}],
        )
        assert store["project-store"]["data"] == PROJECT
        # hydration re-fires with the project: the tray lands
        tray = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {"id": "url", "property": "pathname", "value": "/dashboard/analysis"},
                {"id": "url", "property": "search", "value": search},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[
                {
                    "id": "selection-store",
                    "property": "data",
                    "value": dict(EMPTY_TRAY),
                },
                {"id": "view-store", "property": "data", "value": None},
            ],
        )
        assert tray["selection-store"]["data"]["sweeps"] == [str(SWEEP_A)]
        assert tray["analysis-message-store"]["data"] == ""

    def test_picked_project_is_never_switched_by_a_foreign_token(
        self, authed, callback_map
    ):
        token = encode_selection_token(Selection(project="other", trials=(RA0,)))
        picker = self._dispatch(
            authed,
            callback_map,
            {"project-picker.value"},
            [
                {"id": "project-store", "property": "data", "value": PROJECT},
                {"id": "url", "property": "search", "value": f"?sel={token}"},
            ],
        )
        assert picker["project-picker"]["value"] == PROJECT
        message = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {"id": "url", "property": "pathname", "value": "/dashboard/analysis"},
                {"id": "url", "property": "search", "value": f"?sel={token}"},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[
                {"id": "selection-store", "property": "data", "value": None},
                {"id": "view-store", "property": "data", "value": None},
            ],
        )
        assert "selection-store" not in message
        assert "not the current project" in message["analysis-message-store"]["data"]

    def test_unknown_project_neither_adopts_nor_populates(self, authed, callback_map):
        token = encode_selection_token(Selection(project="ghost", trials=(RA0,)))
        picker = self._dispatch(
            authed,
            callback_map,
            {"project-picker.value"},
            [
                {"id": "project-store", "property": "data", "value": None},
                {"id": "url", "property": "search", "value": f"?sel={token}"},
            ],
        )
        assert picker["project-picker"]["value"] is None
        cold = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {"id": "url", "property": "pathname", "value": "/dashboard/analysis"},
                {"id": "url", "property": "search", "value": f"?sel={token}"},
                {"id": "project-store", "property": "data", "value": None},
            ],
            state=[
                {
                    "id": "selection-store",
                    "property": "data",
                    "value": dict(EMPTY_TRAY),
                },
                {"id": "view-store", "property": "data", "value": None},
            ],
        )
        assert "selection-store" not in cold
        assert "project 'ghost'" in cold["analysis-message-store"]["data"]

    def test_deep_link_settles_view_state_alongside_the_tray(
        self, authed, callback_map
    ):
        doc = dict(default_view_state(), active="series")
        doc["series"] = {**doc["series"], "keys": ["loss"], "reduction": "mean"}
        sel = encode_selection_token(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        search = f"?sel={sel}&view={encode_view_state(doc)}"
        result = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {"id": "url", "property": "pathname", "value": "/dashboard/analysis"},
                {"id": "url", "property": "search", "value": search},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[
                {
                    "id": "selection-store",
                    "property": "data",
                    "value": dict(EMPTY_TRAY),
                },
                {"id": "view-store", "property": "data", "value": None},
            ],
        )
        assert result["selection-store"]["data"]["sweeps"] == [str(SWEEP_A)]
        assert result["view-store"]["data"] == doc

    def test_malformed_view_parameter_defaults_and_errors(self, authed, callback_map):
        sel = encode_selection_token(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        search = "?sel=" + sel + "&view=%zz{"
        result = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {"id": "url", "property": "pathname", "value": "/dashboard/analysis"},
                {"id": "url", "property": "search", "value": search},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[
                {
                    "id": "selection-store",
                    "property": "data",
                    "value": dict(EMPTY_TRAY),
                },
                {"id": "view-store", "property": "data", "value": None},
            ],
        )
        assert result["selection-store"]["data"]["sweeps"] == [str(SWEEP_A)]
        assert result["view-store"]["data"] == default_view_state()
        assert "view state is malformed" in result["analysis-message-store"]["data"]


class TestContinueInPython:
    def test_snippet_uses_real_client_api(self, service):
        page = python_tab(service, PROJECT, _tray())
        snippet = _pres(page)[1].children
        assert snippet.startswith("from jernerics.tracking import TrackingClient")
        assert "from jernerics.tracking.client import decode_selection" in snippet
        assert "TrackingClient(" in snippet
        assert 'decode_selection("' in snippet
        assert f'client.project("{PROJECT}")' in snippet
        token = snippet.split('decode_selection("')[1].split('")')[0]
        import jernerics.tracking as tracking_module
        from jernerics.tracking.client import (
            ProjectHandle,
            TrackingClient,
        )
        from jernerics.tracking.client import (
            decode_selection as client_decode,
        )

        assert tracking_module.TrackingClient is TrackingClient
        assert callable(TrackingClient.project)
        assert callable(ProjectHandle.values)
        decoded = client_decode(token)
        assert decoded == service.analysis_selection(PROJECT, _tray())

    def test_python_snippet_shows_token(self):
        snippet = python_snippet("abc123", PROJECT)
        assert 'decode_selection("abc123")' in snippet


class TestAnalysisRouteServes:
    def test_page_renders_without_polling(self, service):
        page, polls = page_content("/dashboard/analysis", service)
        assert polls is False
        rendered = str(page)
        assert "analysis-selection-store" not in rendered
        assert "Selection" in rendered
        assert "Optuna views" in rendered
        assert "analysis-scope-bar" in rendered
        assert "Edit scope" in rendered
        assert rendered.index("analysis-scope-bar") < rendered.index("analysis-tabs")

    def test_deep_link_with_token_returns_200(self, authed):
        token = encode_selection_token(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        response = authed.get(f"/dashboard/analysis?sel={token}")
        assert response.status_code == 200
        assert "react-entry-point" in response.text
