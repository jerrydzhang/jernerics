"""Cross-sweep analysis views (jernerics-h5d.13).

Callback-layer coverage over a heterogeneous three-sweep seed: token
round-trips, data catalog, series overlay, points tables, study-style
figures, URL hydration, and the continue-in-Python handoff. The
orchestrator browser-drives the mounted dashboard after merge, so these
tests assert on the pure helpers the Dash callbacks wrap plus
TestClient page 200s.
"""

import json
import math
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
    SelectionTokenError,
    SweepSnapshotEvent,
    TrackingEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    decode_selection,
    encode_selection,
)
from jernerics_server.dashboard import analysis, components
from jernerics_server.dashboard.analysis import (
    EMPTY_TRAY,
    VIEW_VERSION,
    ViewStateError,
    apply_context_filters,
    auto_refresh_flip,
    auto_refresh_polls,
    axis_state_edit,
    browser_trial_columns,
    browser_trial_outputs,
    catalog_tab,
    cold_start,
    context_filter_controls,
    control_values,
    decode_view_state,
    default_axis_state,
    default_scope_state,
    default_view_state,
    edited_fields,
    edited_view,
    encode_view_state,
    expand_values,
    hydrate_tray,
    hydrate_view,
    include_values,
    mounted_selection,
    moved_keys,
    param_text,
    points_tab,
    points_view_data,
    python_snippet,
    scope_fingerprint,
    search_from_state,
    series_data_failure,
    series_data_outputs,
    series_outputs,
    series_snapshot,
    series_status,
    series_view_outputs,
    synced_search,
    tray_from_edit,
    tray_summary,
    trial_config_text,
    updated_ago,
    varying_param_keys,
    view_from_context_filter,
    view_from_controls,
    view_from_include,
    view_from_trace_click,
    with_focus,
    workspace_focus_href,
)
from jernerics_server.dashboard.app import build_dash_app
from jernerics_server.dashboard.auth import DashboardContext
from jernerics_server.dashboard.callbacks import page_content, pattern_trigger
from jernerics_server.dashboard.figures import (
    axis_notes,
    clipped_count,
    color_grouping,
    count_note,
    identity_of,
    median_iqr_summary,
    non_positive_count,
    overlay_figure,
    percentile,
    resolve_axis,
    stacked_figure,
)
from jernerics_server.dashboard.layout import shell
from jernerics_server.dashboard.selection_tokens import decode_selection_token
from jernerics_server.dashboard.service import DashboardService
from jernerics_server.dashboard.sessions import SessionSigner
from jernerics_server.dashboard.workspace import active_sweeps, sweep_curation
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
        event(ValueEvent, 868, trial_id=TB, key="delta", step=0, value=-0.5),
        event(ValueEvent, 867, trial_id=TB, key="delta", step=1, value=0.25),
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


def _points_service(
    values: dict[str, dict[str, Any]],
    value_keys: list[dict[str, Any]],
    params: dict[str, dict[str, Any]],
    param_keys: list[str],
) -> Any:
    """Duck-typed service returning one shaped analysis_points payload."""

    class _PointsService:
        def analysis_points(self, project: str | None, tray: dict | None) -> dict:
            return {
                "trials": [
                    {"number": 1, "trial_id": "cc320000-0000-4000-8000-000000000000"}
                ],
                "value_keys": value_keys,
                "values": values,
                "param_keys": param_keys,
                "params": params,
            }

    return _PointsService()


class TestClampPolicy:
    """jernerics-l8f: one truncation policy — single line, ellipsis,
    full value preserved for the tooltip/affordance."""

    def test_short_text_passes_through(self):
        assert components.clamp_text("lr · 3/6") == "lr · 3/6"

    def test_long_text_clamps_with_ellipsis(self):
        clamped = components.clamp_text("x" * 500)
        assert clamped == "x" * (components.TEXT_LIMIT - 1) + components.ELLIPSIS
        assert len(clamped) == components.TEXT_LIMIT

    def test_whitespace_folds_to_one_line(self):
        assert components.clamp_text("a\nb\n---\nc") == "a b --- c"

    def test_tooltip_carries_the_full_value(self):
        full = '{"blob": "' + "x" * 500 + '"}'
        tip = components.clamp_tooltip(full)
        assert getattr(tip, "title", None) == full
        clamped = tip.children
        assert clamped is not None
        assert clamped == components.clamp_text(full)
        assert clamped.endswith(components.ELLIPSIS)


def _pres(page):
    return _walk(page, lambda node: type(node).__name__ == "Pre")


def _selected_trial_ids(service: DashboardService, selection: Selection) -> list[str]:
    return sorted(str(record.trial_id) for record in service.queries.lineage(selection))


class TestSelectionTokens:
    def test_dashboard_round_trip_is_stable(self):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A,), trials=(RA2, TA))
        token = encode_selection(selection)
        assert encode_selection(selection) == token
        assert decode_selection_token(token) == selection

    def test_client_format_token_parses_and_reencodes_identically(self):
        selection = decode_selection_token(CLIENT_FORMAT_TOKEN)
        assert selection == Selection(project=PROJECT, sweeps=(SWEEP_A,))
        assert encode_selection(selection) == CLIENT_FORMAT_TOKEN

    def test_schema_encoded_token_flows_through_both_sides(self):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A,), trials=(RA2,))
        token = encode_selection(selection)
        assert decode_selection_token(token, project=PROJECT) == selection
        assert decode_selection(token) == selection
        assert encode_selection(decode_selection_token(token)) == token

    def test_malformed_token_is_an_error(self):
        with pytest.raises(SelectionTokenError, match="malformed"):
            decode_selection_token("definitely-not-a-token-!!!")

    def test_cross_project_token_surfaces_error_not_mixed_results(self, service):
        with pytest.raises(SelectionTokenError, match="project 'other'"):
            decode_selection_token(CROSS_PROJECT_TOKEN, project=PROJECT)
        tray, error = hydrate_tray(
            service=service,
            project=PROJECT,
            pathname="/dashboard/project/lab",
            search=f"?sel={CROSS_PROJECT_TOKEN}",
            current=None,
        )
        assert tray is None
        assert error is not None and "project" in error


class TestUnifiedSelectionStore:
    def test_token_hydration_feeds_scope_and_summary(self, service):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A, SWEEP_B))
        token = encode_selection(selection)
        scope, error = hydrate_tray(
            service, PROJECT, "/dashboard/project/lab", f"?sel={token}", None
        )
        assert error is None and scope is not None
        assert scope["sweeps"] == [str(SWEEP_A), str(SWEEP_B)]
        assert tray_summary(scope).startswith(f"{len(selection.sweeps or ())} sweeps")
        # The same token against the hydrated scope is a no-op, so the
        # settled store does not rewrite forever.
        again, error = hydrate_tray(
            service, PROJECT, "/dashboard/project/lab", f"?sel={token}", scope
        )
        assert again is None and error is None

    def test_analysis_edits_hit_one_scope(self):
        scope = _edit_tray(
            [{"sweep_id": str(SWEEP_A)}],
            [{"root": str(RA0)}],
            [],
            dict(default_scope_state(), include_archived=True),
        )
        assert scope["sweeps"] == [str(SWEEP_A)]
        assert scope["families"] == [str(RA0)]
        assert scope["include_archived"] is True
        # A later edit replaces families and keeps the rest of the store.
        scope = _edit_tray([{"sweep_id": str(SWEEP_A)}], [], ["expand"], scope)
        assert scope["sweeps"] == [str(SWEEP_A)]
        assert scope["families"] == []
        assert scope["expand"] is True
        assert scope["include_archived"] is True

    def test_points_grid_carries_click_selection(self, service):
        page = points_tab(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
            "score",
        )
        grid = _grids(page)[0]
        assert grid.id == "inv-points-grid"
        assert grid.getRowId == "params.data.tk"
        options = grid.dashGridOptions
        assert options["enableCellTextSelection"] is True
        assert options["ensureDomOrder"] is True
        assert options["pagination"] is False
        assert options["rowSelection"]["mode"] == "multiRow"
        assert options["rowSelection"]["enableClickSelection"] is True


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
            for entry in service.analysis_context_catalog(
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


def _series_doc(**overrides: Any) -> dict:
    doc = default_view_state()
    doc["active"] = "investigations"
    doc["series"].update(overrides)
    return doc


def _all_sweeps() -> dict:
    return _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)])


def _panel_graphs(panels: list) -> list:
    return [node for node in panels if isinstance(node, dcc.Graph)]


def _panel_headers(panels: list) -> list:
    return [node for node in panels if getattr(node, "className", "") == "panel-header"]


class TestSeriesPanels:
    """jernerics-cdf.4: three heterogeneous scalar keys render three
    ordered aligned panels from ONE values read, with independent y
    axes, stable trial colors, and missing coverage visible."""

    def test_three_keys_three_ordered_panels_from_one_read(self, service, monkeypatch):
        doc = _series_doc(keys=["loss", "accuracy", "score"])
        reads: list[tuple[str, ...] | None] = []
        original = service.queries.values

        def spy(selection, **kwargs):
            reads.append(kwargs.get("keys"))
            return original(selection, **kwargs)

        monkeypatch.setattr(service.queries, "values", spy)
        panels, payload, _key_options, _color, _facet = series_outputs(
            service, PROJECT, _all_sweeps(), doc
        )
        assert reads == [("loss", "accuracy", "score")]
        assert set(payload["per_key"]) == {"loss", "accuracy", "score"}
        graph = _panel_graphs(panels)[0]
        titles = [
            annotation.text
            for annotation in graph.figure.layout.annotations
            if annotation.text
        ]
        assert titles[:3] == ["loss", "accuracy", "score"]
        assert graph.figure.layout.xaxis.matches == "x3"
        assert graph.figure.layout.xaxis2.matches == "x3"
        per_panel_traces = [[], [], []]
        for trace in graph.figure.data:
            axis = trace.yaxis or "y"
            per_panel_traces[int(axis.removeprefix("y") or 1) - 1].append(trace)
        assert {trace.name for trace in per_panel_traces[0]} == {
            "cc310000/dd310000",
            "cc310200/dd310100",
            "cc310200/dd310200",
            "cc310300/dd310300",
            "cc320000/dd320000",
        }
        assert [trace.name for trace in per_panel_traces[1]] == ["cc320000/dd320000"]
        assert [trace.name for trace in per_panel_traces[2]] == ["cc330000/dd330000"]

    def test_trial_color_is_stable_across_panels(self, service):
        doc = _series_doc(keys=["loss", "accuracy"])
        panels, *_ = series_outputs(service, PROJECT, _all_sweeps(), doc)
        graph = _panel_graphs(panels)[0]
        by_axis = {}
        for trace in graph.figure.data:
            by_axis.setdefault(trace.yaxis or "y", {})[trace.name] = trace
        loss_tb = by_axis["y"]["cc320000/dd320000"]
        accuracy_tb = by_axis["y2"]["cc320000/dd320000"]
        assert loss_tb.line.color == accuracy_tb.line.color
        identities = {
            trace.name: trace.line.color
            for trace in graph.figure.data
            if trace.yaxis in (None, "y")
        }
        by_trial = {}
        for name, color in identities.items():
            by_trial.setdefault(name.split("/")[0], set()).add(color)
        assert set(by_trial) == {"cc310000", "cc310200", "cc310300", "cc320000"}
        assert all(len(colors) == 1 for colors in by_trial.values())
        assert len({color for colors in by_trial.values() for color in colors}) == 4

    def test_no_reduction_returns_every_point(self, service):
        doc = _series_doc(keys=["loss"])
        panels, *_ = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        assert sum(len(trace.x) for trace in graph.figure.data) == 10

    def test_mean_reduction_folds_executions_per_trial(self, service):
        doc = _series_doc(keys=["loss"], reduction="mean")
        panels, *_ = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        names = {trace.name for trace in graph.figure.data}
        assert names == {"cc310000", "cc310200", "cc310300"}
        merged = next(t for t in graph.figure.data if t.name == "cc310200")
        assert list(merged.x) == [0, 1, 2, 3]
        assert merged.y[0] == pytest.approx((0.5 + 0.6) / 2)
        assert merged.y[3] == pytest.approx(0.35)

    def test_reductions_apply_independently_per_key(self, service):
        doc = _series_doc(keys=["loss", "delta"], reduction="mean")
        panels, *_ = series_outputs(service, PROJECT, _all_sweeps(), doc)
        graph = _panel_graphs(panels)[0]
        by_axis = {}
        for trace in graph.figure.data:
            by_axis.setdefault(trace.yaxis or "y", []).append(trace)
        assert [t.name for t in by_axis["y2"]] == ["cc320000"]
        assert list(by_axis["y2"][0].y) == pytest.approx([-0.5, 0.25])

    def test_picker_options_show_kind_coverage_and_extent(self, service):
        doc = _series_doc(keys=[])
        _panels, _payload, key_options, color_options, facet_options = series_outputs(
            service, PROJECT, _all_sweeps(), doc
        )
        by_value = {option["value"]: option for option in key_options}
        assert by_value["loss"]["label"] == "loss · 12 pts · 4 trials"
        assert by_value["loss"]["title"] == (
            "scalar · 12 points · 4 trials · 3 families · steps 0-3"
        )
        assert "steps 0-1" in by_value["accuracy"]["title"]
        assert "delta" in by_value
        assert "score" not in by_value
        values = {option["value"] for option in color_options}
        assert values >= {"host", "shard", "param:lr", "param:seed"}
        labels = {option["value"]: option["label"] for option in color_options}
        assert labels["param:lr"].startswith("param lr")
        assert {option["value"] for option in facet_options} == {"host", "shard"}

    def test_non_scalar_keys_stay_unselected_and_absent_keys_are_kept(
        self,
        service,
    ):
        doc = _series_doc(keys=["summary", "ghost"])
        panels, _payload, key_options, *_unused = series_outputs(
            service, PROJECT, _all_sweeps(), doc
        )
        labels = {option["value"]: option["label"] for option in key_options}
        assert labels["ghost"] == "ghost · absent under this scope"
        headers = _panel_headers(panels)
        rendered = str(headers)
        assert "summary" in rendered and "ghost" in rendered
        assert "no observations under this scope" in rendered

    def test_missing_key_keeps_its_panel_with_coverage_note(self, service):
        doc = _series_doc(keys=["loss", "score"])
        panels, payload, _k, _c, _f = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        titles = [
            annotation.text
            for annotation in graph.figure.layout.annotations
            if annotation.text
        ]
        assert titles[:2] == ["loss", "score"]
        assert payload["per_key"]["score"]["series"] == []
        assert "no observations under this scope" in str(_panel_headers(panels))

    def test_context_color_keys_traces_by_dimension(self, service):
        doc = _series_doc(keys=["loss"], color="shard")
        panels, *_ = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        assert len(graph.figure.data) == 4
        by_trial = {trace.customdata[0]: trace for trace in graph.figure.data}
        shard_zero = [
            by_trial[str(RA0)],
            next(
                trace
                for trace in graph.figure.data
                if trace.customdata[0] == str(RA2) and trace.line.dash == "solid"
            ),
            by_trial[str(TA)],
        ]
        assert len({trace.line.color for trace in shard_zero}) == 1
        assert (
            next(
                trace
                for trace in graph.figure.data
                if trace.customdata[0] == str(RA2) and trace.line.dash != "solid"
            ).line.color
            != shard_zero[0].line.color
        )
        assert len({trace.line.color for trace in graph.figure.data}) == 2
        legend_names = {trace.name for trace in graph.figure.data if trace.showlegend}
        assert legend_names == {"0", "1"}

    def test_execution_dash_distinguishes_executions_of_one_trial(self, service):
        doc = _series_doc(keys=["loss"])
        panels, *_ = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        by_name = {trace.name: trace for trace in graph.figure.data}
        first = by_name["cc310200/dd310100"].line.dash
        second = by_name["cc310200/dd310200"].line.dash
        assert first != second
        assert by_name["cc310000/dd310000"].line.dash == "solid"

    def test_projectless_renders_empty_state_and_empty_payload(self):
        panels, payload, key_options, _color, _facet = series_outputs(
            None, None, None, _series_doc(keys=["loss"])
        )
        assert "Pick a project" in str(panels)
        assert key_options == []
        assert payload["fingerprint"] == ""
        assert payload["per_key"] == {"loss": {"series": []}}

    def test_no_keys_renders_guidance(self, service):
        panels, *_rest = series_outputs(service, PROJECT, _all_sweeps(), _series_doc())
        assert "No value keys selected" in str(panels)


class TestPointsView:
    """The Points view: trials x final scalars with the params ->
    outcome parallel coordinates; line order equals row order and
    dimension ranges always cover every line."""

    def test_rows_carry_final_scalars_and_raw_sort_fields(self, service):
        page = points_tab(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
            "score",
        )
        grid = _grids(page)[0]
        by_tk = {row["tk"]: row for row in grid.rowData}
        assert len(grid.rowData) == 6
        # Sweep C's trial has the non-step "score" final.
        assert by_tk[str(TC)]["score"] == "0.78"
        assert by_tk[str(TC)]["score_raw"] == pytest.approx(0.78)
        # Sweep A's trials have no non-step key: the missing marker.
        assert by_tk[str(RA0)]["score"] == components.MISSING
        assert by_tk[str(RA0)]["score_raw"] is None

    def test_value_columns_sort_numerically_over_raw_fields(self, service):
        page = points_tab(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)]),
            "score",
        )
        grid = _grids(page)[0]
        score = next(c for c in grid.columnDefs if c["field"] == "score")
        assert score["comparator"] == {
            "function": "renderTypedSort('numeric', 'score_raw')"
        }

    def test_dims_cover_params_sweep_and_outcome(self, service):
        trials = service.analysis_trials(
            PROJECT, _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)])
        )
        keys = analysis.points_scalar_keys(
            service, PROJECT, _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)])
        )
        finals = service.analysis_finals(
            PROJECT, _tray(sweeps=[str(SWEEP_A), str(SWEEP_B), str(SWEEP_C)])
        )
        view = points_view_data(trials, keys, finals, "score")
        assert [row["tk"] for row in view["rows"]] == view["tks"]
        labels = [dim["label"] for dim in view["dims"]]
        assert labels[-1] == "score (final)"
        assert labels[-2] == "sweep"
        # lr varies and is numeric; seed is missing on some trials but
        # varies by presence, so both are numeric dims.
        assert "lr" in labels
        sweep_dim = view["dims"][-2]
        assert sweep_dim["ticktext"] == sorted(set(sweep_dim["ticktext"]))
        assert len(view["dims"][-1]["values"]) == len(view["tks"])
        for dim in view["dims"]:
            present = [v for v in dim["values"] if not math.isnan(v)]
            if present:
                assert dim["range"][0] <= min(present)
                assert dim["range"][1] >= max(present)
        assert view["with_outcome"] == 1

    def test_no_numeric_outcome_leaves_an_honest_empty_state(self, service):
        page = points_tab(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A)]),
            "absent_key",
        )
        assert "numeric final values" in str(page)

    def test_member_scope_narrows_the_rows(self, service):
        single = points_tab(service, PROJECT, _tray(sweeps=[str(SWEEP_C)]), "score")
        grid = _grids(single)[0]
        assert [row["tk"] for row in grid.rowData] == [str(TC)]


class TestUrlReload:
    def test_token_hydrates_equal_tray_selection(self, service):
        selection = Selection(project=PROJECT, sweeps=(SWEEP_A,), trials=(RA2,))
        search = f"?sel={encode_selection(selection)}"
        scope, error = hydrate_tray(
            service, PROJECT, "/dashboard/project/lab", search, None
        )
        assert error is None and scope is not None
        assert service.analysis_selection(PROJECT, scope) == selection
        assert expand_values({"scope": scope}) == []

    def test_retry_root_token_hydrates_with_expansion(self, service):
        selection = Selection(project=PROJECT, retry_roots=(RA0,))
        scope, error = hydrate_tray(
            service,
            PROJECT,
            "/dashboard/project/lab",
            f"?sel={encode_selection(selection)}",
            None,
        )
        assert error is None and scope is not None
        assert expand_values({"scope": scope}) == ["expand"]
        assert _selected_trial_ids(
            service, service.analysis_selection(PROJECT, scope)
        ) == _selected_trial_ids(service, selection)

    def test_scope_round_trips_through_the_view_parameter(self, service):
        scope = _edit_tray([], [{"root": str(RA0)}], ["expand"], None)
        doc = edited_view(default_view_state(), {"scope": scope})
        search = search_from_state(doc, "")
        assert search is not None and search.startswith("?view=")
        hydrated, error = hydrate_view("/dashboard/project/lab", search, None)
        assert error is None and hydrated is not None
        assert service.analysis_selection(PROJECT, hydrated["scope"]) == (
            service.analysis_selection(PROJECT, scope)
        )
        assert search_from_state(hydrated, search) is None

    def test_non_analysis_url_is_ignored(self, service):
        tray, error = hydrate_tray(
            service,
            PROJECT,
            "/dashboard/",
            f"?sel={encode_selection(Selection(project=PROJECT))}",
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
        adopted, error = cold_start(service, f"?sel={encode_selection(selection)}")
        assert error is None and adopted == selection

    def test_unknown_project_token_names_itself_in_the_hint(self, service):
        token = encode_selection(Selection(project="ghost", trials=(RA0,)))
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
        token = encode_selection(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        search = f"?sel={token}"
        tray, error = hydrate_tray(
            service, None, "/dashboard/project/lab", search, dict(EMPTY_TRAY)
        )
        assert tray is None and error is None
        adopted, _error = cold_start(service, search)
        assert adopted is not None and adopted.project == PROJECT
        hydrated, error = hydrate_tray(
            service, adopted.project, "/dashboard/project/lab", search, dict(EMPTY_TRAY)
        )
        assert error is None and hydrated is not None
        assert hydrated["sweeps"] == [str(SWEEP_A)]
        assert hydrate_tray(
            service, adopted.project, "/dashboard/project/lab", search, hydrated
        ) == (None, None)

    def test_unknown_project_token_hints_on_the_analysis_page(self, service):
        token = encode_selection(Selection(project="ghost", trials=(RA0,)))
        tray, error = hydrate_tray(
            service, None, "/dashboard/project/lab", f"?sel={token}", dict(EMPTY_TRAY)
        )
        assert tray is None
        assert error is not None and "project 'ghost'" in error

    def test_invalid_token_on_fresh_session_still_errors(self, service):
        tray, error = hydrate_tray(
            service,
            None,
            "/dashboard/project/lab",
            "?sel=definitely-not-a-token-!!!",
            dict(EMPTY_TRAY),
        )
        assert tray is None
        assert error is not None and "malformed" in error

    def test_off_analysis_url_neither_adopts_nor_errors(self, service):
        token = encode_selection(Selection(project=PROJECT))
        assert hydrate_tray(
            service, None, "/dashboard/", f"?sel={token}", dict(EMPTY_TRAY)
        ) == (None, None)


class TestUrlSync:
    """jernerics-8c9/2se: one shell-only callback owns ``url.search`` —
    view edits mint ``?view=`` on the investigation page only (the
    workspace page owns its search parameters outright), a navigation
    drops only the parameters of pages that no longer mint, and a
    navigation never mints (a stale document would clobber a freshly
    opened deep link before hydration lands)."""

    @staticmethod
    def _doc(scope=None, **changes):
        if scope is not None:
            changes["scope"] = scope
        return edited_view(default_view_state(), changes)

    def test_view_edit_mints_on_the_investigation_page(self, service):
        scope = _edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None)
        target = synced_search(
            "/dashboard/project/lab/investigation/inv-1",
            self._doc(scope),
            "",
            url_navigated=False,
        )
        assert target is not None and target.startswith("?view=")
        doc = decode_view_state(unquote(target.removeprefix("?view=")))
        assert service.analysis_selection(PROJECT, doc["scope"]) == (
            service.analysis_selection(PROJECT, scope)
        )

    def test_view_edit_on_workspace_leaves_the_search_alone(self, service):
        doc = self._doc(_edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None))
        assert (
            synced_search("/dashboard/project/lab", doc, "", url_navigated=False)
            is None
        )
        assert (
            synced_search("/dashboard/project/lab", doc, "?view=x", url_navigated=False)
            is None
        )

    def test_unchanged_state_leaves_the_search_alone(self, service):
        doc = self._doc(_tray())
        search = search_from_state(doc, "")
        assert (
            synced_search(
                "/dashboard/project/lab/investigation/inv-1",
                doc,
                search,
                url_navigated=False,
            )
            is None
        )

    def test_view_edit_off_analytic_pages_leaves_the_search_alone(self, service):
        doc = self._doc(_edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None))
        assert synced_search("/dashboard/", doc, "?view=x", url_navigated=False) is None

    def test_navigation_off_workspace_strips_the_parameter(self, service):
        assert (
            synced_search(
                "/dashboard/", default_view_state(), "?view=x", url_navigated=True
            )
            == ""
        )

    def test_navigation_with_no_search_leaves_the_url_alone(self, service):
        assert (
            synced_search("/dashboard/", default_view_state(), "", url_navigated=True)
            is None
        )

    def test_navigation_onto_workspace_never_mints_over_a_deep_link(self, service):
        doc = self._doc(_edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None))
        deep_link = "?view=" + encode_view_state(
            self._doc(_edit_tray([{"sweep_id": str(SWEEP_B)}], [], [], None))
        )
        assert (
            synced_search(
                "/dashboard/project/lab",
                doc,
                deep_link,
                url_navigated=True,
            )
            is None
        )

    def test_pre_picked_deep_link_survives_the_mount_echo(self, service):
        """The 8c9 regression ordering, as one callback sequence: with a
        session project already settled, a deep link hydrates while the
        picker grids are still mounting; AG Grid's stale mount echo then
        reports empty picks. The scope survives and ?view= is neither
        stripped nor rewritten."""
        session_doc = default_view_state()
        search = "?view=" + encode_view_state(
            self._doc(_edit_tray([{"sweep_id": str(SWEEP_A)}], [], [], None))
        )
        hydrated, error = hydrate_view("/dashboard/project/lab", search, session_doc)
        assert error is None and hydrated is not None
        assert hydrated["scope"]["sweeps"] == [str(SWEEP_A)]
        # loaders' initial call: empty selection is not pushed to the
        # mounting grids, so no empty-selection echo can fire
        assert mounted_selection([], initial=True) is no_update
        # the stale echo (family grid mounted, sweep grid not yet applied)
        echo_scope = tray_from_edit(
            [],
            [],
            [],
            hydrated["scope"],
            sweep_edited=False,
            family_edited=True,
            expand_edited=False,
        )
        echo = edited_view(hydrated, {"scope": echo_scope})
        assert echo == hydrated  # equality guard prevents the edit
        assert (
            synced_search("/dashboard/project/lab", echo, search, url_navigated=False)
            is None
        )

    def test_cold_start_deep_link_settles_through_adoption(self, service):
        """jernerics-xbx ordering, as one callback sequence: the picker
        adopts the token's project, project-store settles through the
        picker's own remember callback (the manual-pick path), hydration
        re-fires with a project and the scope lands — the settle re-fire
        is a no-op, the grid echo edits nothing, and the URL keeps the
        token (the workspace page never mints ?view= over it)."""
        search = "?sel=" + encode_selection(
            Selection(project=PROJECT, sweeps=(SWEEP_A,))
        )
        adoption, error = cold_start(service, search)
        assert error is None and adoption is not None
        assert adoption.project == PROJECT
        # project-store settled: hydration re-fires with the project
        scope, error = hydrate_tray(
            service,
            adoption.project,
            "/dashboard/project/lab",
            search,
            default_scope_state(),
        )
        assert error is None and scope is not None
        assert scope["sweeps"] == [str(SWEEP_A)]
        # the settle re-fire against the hydrated scope rewrites nothing
        assert hydrate_tray(
            service, PROJECT, "/dashboard/project/lab", search, scope
        ) == (None, None)
        # a grid echo against the settled scope edits nothing
        echo = tray_from_edit(
            [],
            [],
            [],
            scope,
            sweep_edited=False,
            family_edited=True,
            expand_edited=False,
        )
        assert echo == scope
        # the workspace page owns its search: the rewrite leaves the
        # token in place instead of minting ?view= over it
        assert (
            synced_search(
                "/dashboard/project/lab",
                self._doc(scope),
                search,
                url_navigated=False,
            )
            is None
        )


class TestViewStateCodec:
    """jernerics-cdf.3/2se: the dashboard-only ``view=`` document —
    version, enums, JSON types, unknown-field dropping, defaults for
    missing fields, percent-encoded compact JSON round-trips, the
    defaults-diff encoding, and legacy full-document decode."""

    def test_round_trip_is_exact_and_url_safe(self):
        doc = default_view_state()
        doc["active"] = "investigations"
        doc["series"] = {
            **doc["series"],
            "keys": ["loss", "accuracy", "delta"],
            "mode": "overlay",
            "reduction": "mean",
            "trial_display": "highlighted",
            "context_filters": {"host": ["node00", "node01"]},
            "color": "shard",
            "axes": {
                "loss": {
                    "scale": "log",
                    "range": "custom",
                    "min": 1.0,
                    "max": 100.0,
                },
                "accuracy": {
                    "scale": "linear",
                    "range": "auto",
                    "min": None,
                    "max": None,
                },
            },
            "overlay_axis": {
                "scale": "linear",
                "range": "custom",
                "min": -1.0,
                "max": 2.0,
            },
        }
        doc["highlighted_trials"] = [str(RA0)]
        doc["auto_refresh"] = True
        doc["optuna"] = {"contour_x": "lr", "contour_y": "seed"}
        encoded = encode_view_state(doc)
        assert all(char not in '{}":,&' for char in encoded)
        assert decode_view_state(unquote(encoded)) == doc
        assert encode_view_state(decode_view_state(unquote(encoded))) == encoded

    def test_default_mode_is_stacked_and_axes_default_to_linear_auto(self):
        assert default_view_state()["series"]["mode"] == "stacked"
        assert default_view_state()["series"]["axes"] == {}
        assert default_view_state()["series"]["overlay_axis"] == (default_axis_state())
        assert default_axis_state() == {
            "scale": "linear",
            "range": "auto",
            "min": None,
            "max": None,
        }

    def test_absent_axis_entry_means_linear_auto(self):
        doc = decode_view_state(
            json.dumps({"v": VIEW_VERSION, "series": {"keys": ["loss"]}})
        )
        assert doc["series"]["axes"] == {}

    def test_auto_range_ignores_stored_bounds_and_unknown_axis_fields(self):
        decoded = decode_view_state(
            json.dumps(
                {
                    "v": VIEW_VERSION,
                    "series": {
                        "axes": {
                            "loss": {
                                "range": "auto",
                                "min": 3,
                                "max": 9,
                                "dash": "dot",
                            }
                        },
                        "overlay_axis": {"scale": "log", "bogus": 1},
                    },
                }
            )
        )
        assert decoded["series"]["axes"]["loss"] == default_axis_state()
        assert decoded["series"]["overlay_axis"] == {
            "scale": "log",
            "range": "auto",
            "min": None,
            "max": None,
        }

    def test_old_string_axes_form_is_rejected_without_alias(self):
        with pytest.raises(ViewStateError, match="must be an object"):
            decode_view_state(json.dumps({"v": 1, "series": {"axes": {"loss": "y1"}}}))

    def test_unknown_fields_are_dropped_and_not_re_emitted(self):
        doc = default_view_state()
        doc["active"] = "investigations"
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
        partial = json.dumps({"v": VIEW_VERSION, "active": "investigations"})
        assert decode_view_state(partial)["active"] == "investigations"
        assert decode_view_state(partial)["series"]["keys"] == []

    @pytest.mark.parametrize(
        "payload",
        [
            "not json",
            "[1, 2]",
            json.dumps({"v": 3}),
            json.dumps({"v": True}),
            json.dumps({"v": VIEW_VERSION, "scope": []}),
            json.dumps({"v": VIEW_VERSION, "scope": {"sweeps": 7}}),
            json.dumps({"v": VIEW_VERSION, "scope": {"expand": "yes"}}),
            json.dumps({"v": VIEW_VERSION, "scope": {"include_archived": 1}}),
            json.dumps({"v": VIEW_VERSION, "scope": {"families": [""]}}),
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
            json.dumps({"v": VIEW_VERSION, "series": {"axes": {"loss": "y1"}}}),
            json.dumps(
                {
                    "v": VIEW_VERSION,
                    "series": {"axes": {"loss": {"range": "custom", "min": 1}}},
                }
            ),
            json.dumps(
                {
                    "v": VIEW_VERSION,
                    "series": {"axes": {"loss": {"range": "custom", "min": 5}}},
                }
            ),
            json.dumps(
                {
                    "v": VIEW_VERSION,
                    "series": {
                        "axes": {"loss": {"range": "custom", "min": 2, "max": 1}}
                    },
                }
            ),
            json.dumps(
                {
                    "v": VIEW_VERSION,
                    "series": {
                        "axes": {
                            "loss": {
                                "scale": "log",
                                "range": "custom",
                                "min": 0,
                                "max": 5,
                            }
                        }
                    },
                }
            ),
            json.dumps(
                {
                    "v": VIEW_VERSION,
                    "series": {
                        "axes": {"loss": {"range": "custom", "min": "a", "max": 5}}
                    },
                }
            ),
            json.dumps(
                {"v": VIEW_VERSION, "series": {"axes": {"loss": {"scale": "ln"}}}}
            ),
            json.dumps(
                {
                    "v": VIEW_VERSION,
                    "series": {
                        "overlay_axis": {
                            "scale": "log",
                            "range": "custom",
                            "min": -1,
                            "max": 5,
                        }
                    },
                }
            ),
            json.dumps({"v": VIEW_VERSION, "series": {"axes": {"loss": 1}}}),
            json.dumps({"v": VIEW_VERSION, "highlighted_trials": [7]}),
            json.dumps({"v": VIEW_VERSION, "auto_refresh": "yes"}),
            json.dumps({"v": VIEW_VERSION, "optuna": {"contour_x": 4}}),
        ],
    )
    def test_bad_payloads_fail_with_descriptive_errors(self, payload):
        with pytest.raises(ViewStateError):
            decode_view_state(payload)

    def test_error_messages_name_the_problem(self):
        with pytest.raises(ViewStateError, match="expected version 2"):
            decode_view_state(json.dumps({"v": 3}))
        with pytest.raises(ViewStateError, match="unsupported analysis view"):
            decode_view_state(json.dumps({"v": 1, "active": "selection"}))
        with pytest.raises(ViewStateError, match="malformed"):
            decode_view_state("%zz{")

    def test_duplicate_keys_keep_first_occurrence_order(self):
        doc = default_view_state()
        doc["series"]["keys"] = ["loss", "accuracy", "loss"]
        decoded = decode_view_state(json.dumps(doc))
        assert decoded["series"]["keys"] == ["loss", "accuracy"]

    def test_encode_emits_only_non_default_fields(self):
        assert unquote(encode_view_state(default_view_state())) == '{"v":2}'
        doc = with_focus(default_view_state(), {"kind": "sweep", "id": str(SWEEP_A)})
        payload = json.loads(unquote(encode_view_state(doc)))
        assert payload == {
            "v": VIEW_VERSION,
            "focus": {"kind": "sweep", "id": str(SWEEP_A)},
        }
        assert len(encode_view_state(doc)) < 200

    def test_legacy_v1_document_decodes_to_the_same_effective_state(self):
        legacy = {
            "v": 1,
            "active": "investigations",
            "series": {
                "keys": ["loss"],
                "mode": "stacked",
                "reduction": "none",
                "trial_display": "all",
                "context_filters": {},
                "color": None,
                "facet": None,
                "axes": {},
                "overlay_axis": default_axis_state(),
            },
            "highlighted_trials": [],
            "focus": None,
            "auto_refresh": False,
            "include_archived": True,
            "include_invalid": False,
            "optuna": {"contour_x": None, "contour_y": None},
        }
        from_legacy = decode_view_state(json.dumps(legacy))
        diff = {
            "v": VIEW_VERSION,
            "active": "investigations",
            "scope": {"include_archived": True},
            "series": {"keys": ["loss"]},
        }
        assert decode_view_state(json.dumps(diff)) == from_legacy
        # the re-encode is a minimal defaults-diff: no default field rides
        payload = set(json.loads(unquote(encode_view_state(from_legacy))))
        assert payload == {"v", "active", "scope", "series"}

    def test_legacy_v1_scope_never_carries_sweeps(self):
        """v1 kept selection in the session tray; its tokens decode with
        an empty scope, and the include flags move into it."""
        legacy = {
            "v": 1,
            "include_archived": False,
            "include_invalid": True,
            "series": {},
        }
        decoded = decode_view_state(json.dumps(legacy))
        expected = default_view_state()
        expected["scope"]["include_invalid"] = True
        assert decoded == expected


class TestViewHydration:
    def test_valid_document_lands_in_the_store(self):
        doc = dict(default_view_state(), active="investigations")
        hydrated, error = hydrate_view(
            "/dashboard/project/lab", f"?view={encode_view_state(doc)}", None
        )
        assert error is None and hydrated == doc

    def test_equal_state_is_left_alone(self):
        doc = dict(default_view_state(), active="investigations")
        search = f"?view={encode_view_state(doc)}"
        assert hydrate_view("/dashboard/project/lab", search, doc) == (None, None)

    def test_no_parameter_means_defaults(self):
        assert hydrate_view("/dashboard/project/lab", "?sel=tok", None) == (
            default_view_state(),
            None,
        )
        doc = dict(default_view_state(), active="points")
        assert hydrate_view("/dashboard/project/lab", "", doc) == (
            default_view_state(),
            None,
        )

    def test_malformed_document_defaults_with_visible_error(self):
        hydrated, error = hydrate_view(
            "/dashboard/project/lab", "?view=%7Bbroken", dict(default_view_state())
        )
        assert hydrated == default_view_state()
        assert error is not None and "view state" in error

    def test_off_analysis_route_the_store_is_untouched(self):
        doc = dict(default_view_state(), active="points")
        assert hydrate_view("/dashboard/", "?view=%7Bbroken", doc) == (
            None,
            None,
        )


class TestViewSync:
    """jernerics-cdf.3/2se: one shell callback owns ``url.search`` and
    mints ``?view=``; defaults stay out of the URL, and undecodable
    garbage is never silently rewritten away."""

    def test_view_edit_mints_the_parameter(self, service):
        doc = dict(default_view_state(), active="investigations")
        target = search_from_state(doc, "")
        assert target is not None and target.startswith("?view=")
        assert decode_view_state(unquote(target.removeprefix("?view="))) == doc

    def test_default_view_state_is_not_emitted(self, service):
        assert search_from_state(default_view_state(), "") is None

    def test_unchanged_state_is_not_rewritten(self, service):
        doc = dict(default_view_state(), active="investigations")
        target = search_from_state(doc, "")
        assert target is not None
        assert search_from_state(doc, target) is None

    def test_undecodable_view_parameter_is_left_in_place(self, service):
        assert search_from_state(default_view_state(), "?view=%zz{") is None

    _ALL_FIELDS = {
        "active",
        "keys",
        "mode",
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
            "axes": {"loss": {"scale": "log", "range": "auto"}},
            "overlay_axis": {"scale": "linear", "range": "custom", "min": 0, "max": 1},
            "context_filters": {"host": ["node00"]},
        }
        doc["auto_refresh"] = True
        edited = view_from_controls(
            doc,
            active="investigations",
            keys=["accuracy", "score"],
            mode="overlay",
            reduction="max",
            color="shard",
            facet=None,
            contour_x="lr",
            contour_y="seed",
            edited=self._ALL_FIELDS,
        )
        assert edited["active"] == "investigations"
        assert edited["series"]["keys"] == ["accuracy", "score"]
        assert edited["series"]["mode"] == "overlay"
        assert edited["series"]["reduction"] == "max"
        assert edited["series"]["axes"] == {"loss": {"scale": "log", "range": "auto"}}
        assert edited is not None
        assert edited["series"]["overlay_axis"] == {
            "scale": "linear",
            "range": "custom",
            "min": 0,
            "max": 1,
        }
        assert edited["series"]["context_filters"] == {"host": ["node00"]}
        assert edited["auto_refresh"] is True

    def test_keys_edit_dedupes_and_drops_empties(self):
        junk_keys: list[Any] = ["loss", "", "accuracy", "loss", None]
        edited = view_from_controls(
            default_view_state(),
            active=None,
            keys=junk_keys,
            mode=None,
            reduction=None,
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            edited={"keys"},
        )
        assert edited["series"]["keys"] == ["loss", "accuracy"]

    def test_mode_switch_keeps_dormant_per_key_axes(self):
        doc = _series_doc(
            keys=["loss", "accuracy"],
            axes={"loss": {"scale": "log", "range": "auto"}},
        )
        overlaid = view_from_controls(
            doc,
            active="investigations",
            keys=["loss", "accuracy"],
            mode="overlay",
            reduction=None,
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            edited={"mode"},
        )
        assert overlaid["series"]["mode"] == "overlay"
        assert overlaid["series"]["axes"] == doc["series"]["axes"]
        restored = view_from_controls(
            overlaid,
            active="investigations",
            keys=["loss", "accuracy"],
            mode="stacked",
            reduction=None,
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            edited={"mode"},
        )
        assert restored["series"]["axes"] == doc["series"]["axes"]
        assert restored["series"]["mode"] == "stacked"

    def test_untriggered_controls_never_read_as_clears(self):
        """The control-sync write fires the edit callback with every
        input; a dropdown whose options have not loaded reports None and
        must not wipe the hydrated keys."""
        doc: dict[str, Any] = dict(default_view_state(), active="investigations")
        doc["series"] = {
            **doc["series"],
            "keys": ["loss"],
            "color": "shard",
            "reduction": "mean",
        }
        tab_echo = view_from_controls(
            doc,
            active="investigations",
            keys=None,
            mode=None,
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
        assert edited_fields({"analysis-mode.value"}) == {"mode"}
        assert edited_fields({"url.search"}) == set()

    _LOADED: dict[str, set[str] | None] = {
        "keys": {"loss", "accuracy", "summary"},
        "color": {"host", "shard"},
        "facet": {"host", "shard"},
        "contour_x": {"lr", "seed"},
        "contour_y": {"lr", "seed"},
    }

    def test_control_values_read_the_ordered_series_keys(self):
        doc = default_view_state()
        doc["series"]["keys"] = ["accuracy", "loss"]
        active, keys, mode, reduction, color, facet, cx, cy, display, auto = (
            control_values(
                doc,
                self._LOADED,
            )
        )
        assert (active, keys, mode, reduction) == (
            "overview",
            ["accuracy", "loss"],
            "stacked",
            "none",
        )
        assert (color, facet, cx, cy) == (None, None, None, None)
        assert (display, auto) == ("all", [])
        assert control_values(None, self._LOADED)[1] == []
        doc["series"]["trial_display"] = "median_iqr"
        doc["auto_refresh"] = True
        assert control_values(doc, self._LOADED)[8:] == ("median_iqr", ["auto"])

    def test_values_wait_for_their_options(self):
        """A value written before its options exist is dropped by the
        dropdown and fires back as a spurious clear — gating keeps it
        for the options-arrival write."""
        doc = default_view_state()
        doc["series"] = {**doc["series"], "keys": ["loss"], "color": "shard"}
        doc["optuna"] = {**doc["optuna"], "contour_x": "lr"}
        unloaded: dict[str, set[str] | None] = {name: None for name in self._LOADED}
        _a, keys, _m, _r, color, _f, cx, _cy, _d, _ar = control_values(doc, unloaded)
        assert keys is no_update
        assert color is no_update
        assert cx is no_update
        partial = {**self._LOADED, "keys": set()}
        _a, keys, _m, _r, _c, _f, cx, _cy, _d, _ar = control_values(doc, partial)
        assert keys is no_update
        assert cx == "lr"


class TestEntryPoints:
    """Doors back into the focused workspace: the artifact viewer's
    back-links and the shell's view store."""

    def test_focus_href_scopes_to_exactly_that_object(self):
        href = workspace_focus_href(PROJECT, "sweep", str(SWEEP_A))
        assert href.startswith("/dashboard/project/lab?view=")
        doc = decode_view_state(unquote(href.split("?view=")[1]))
        assert doc["focus"] == {"kind": "sweep", "id": str(SWEEP_A)}

    def test_focus_href_needs_no_scope(self):
        href = workspace_focus_href(PROJECT, "trial", str(SWEEP_B))
        assert "sel=" not in href
        doc = decode_view_state(unquote(href.split("?view=")[1]))
        assert doc["focus"]["kind"] == "trial"
        assert doc["series"]["keys"] == []

    def test_view_store_starts_at_defaults(self):
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
            # A callback whose outputs are all page-local is pruned on
            # every other page, so its states may be page-local too.
            if output_ids and not (output_ids & shell_ids):
                continue
            states = {dep["id"] for dep in spec.get("state", [])}
            assert states <= shell_ids, key


class TestColdStartMountedJourney:
    """jernerics-xbx over the mounted app: the registered callbacks,
    driven through Dash's own dispatch endpoint exactly as the browser
    would on a fresh deep link — the picker adopts the token's project,
    the remember callback settles project-store, hydration re-fires and
    the scope lands in the view doc."""

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
        outputs = []
        for spec in specs:
            prop = spec.rsplit(".", 1)[1]
            raw = spec.rsplit(".", 1)[0]
            if raw.startswith("{"):
                wildcard = json.loads(raw)
                concrete = {
                    name: "canvas" if values == ["ALL"] else values
                    for name, values in wildcard.items()
                }
                outputs.append({"id": concrete, "property": prop})
            else:
                outputs.append({"id": raw, "property": prop})
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
        "analysis-message-store.data",
        "view-store.data",
    }

    def test_cold_start_settles_picker_store_then_scope(self, authed, callback_map):
        token = encode_selection(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
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
            state=[
                {"id": "project-picker", "property": "value", "value": None},
            ],
        )
        assert picker["project-picker"]["value"] == PROJECT
        # the remember callback settles project-store (the manual-pick path)
        store = self._dispatch(
            authed,
            callback_map,
            {"project-store.data"},
            [{"id": "project-picker", "property": "value", "value": PROJECT}],
            state=[{"id": "project-store", "property": "data", "value": None}],
        )
        assert store["project-store"]["data"] == PROJECT
        # hydration re-fires with the project: the scope lands in the doc
        result = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {
                    "id": "url",
                    "property": "pathname",
                    "value": "/dashboard/project/lab",
                },
                {"id": "url", "property": "search", "value": search},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[{"id": "view-store", "property": "data", "value": None}],
        )
        assert result["view-store"]["data"]["scope"]["sweeps"] == [str(SWEEP_A)]
        assert result["analysis-message-store"]["data"] == ""

    def test_picked_project_is_never_switched_by_a_foreign_token(
        self, authed, callback_map
    ):
        token = encode_selection(Selection(project="other", trials=(RA0,)))
        picker = self._dispatch(
            authed,
            callback_map,
            {"project-picker.value"},
            [
                {"id": "project-store", "property": "data", "value": PROJECT},
                {"id": "url", "property": "search", "value": f"?sel={token}"},
            ],
            state=[
                {"id": "project-picker", "property": "value", "value": None},
            ],
        )
        assert picker["project-picker"]["value"] == PROJECT
        message = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {
                    "id": "url",
                    "property": "pathname",
                    "value": "/dashboard/project/lab",
                },
                {"id": "url", "property": "search", "value": f"?sel={token}"},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[{"id": "view-store", "property": "data", "value": None}],
        )
        assert "not the current project" in message["analysis-message-store"]["data"]
        assert message["view-store"]["data"]["scope"]["sweeps"] == []

    def test_unknown_project_neither_adopts_nor_populates(self, authed, callback_map):
        token = encode_selection(Selection(project="ghost", trials=(RA0,)))
        picker = self._dispatch(
            authed,
            callback_map,
            {"project-picker.value"},
            [
                {"id": "project-store", "property": "data", "value": None},
                {"id": "url", "property": "search", "value": f"?sel={token}"},
            ],
            state=[
                {"id": "project-picker", "property": "value", "value": None},
            ],
        )
        assert picker["project-picker"]["value"] is None
        cold = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {
                    "id": "url",
                    "property": "pathname",
                    "value": "/dashboard/project/lab",
                },
                {"id": "url", "property": "search", "value": f"?sel={token}"},
                {"id": "project-store", "property": "data", "value": None},
            ],
            state=[{"id": "view-store", "property": "data", "value": None}],
        )
        assert "project 'ghost'" in cold["analysis-message-store"]["data"]
        assert cold["view-store"]["data"]["scope"]["sweeps"] == []

    def test_deep_link_settles_view_state_alongside_the_tray(
        self, authed, callback_map
    ):
        doc: dict[str, Any] = dict(default_view_state(), active="investigations")
        doc["series"] = {**doc["series"], "keys": ["loss"], "reduction": "mean"}
        sel = encode_selection(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        search = f"?sel={sel}&view={encode_view_state(doc)}"
        result = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {
                    "id": "url",
                    "property": "pathname",
                    "value": "/dashboard/project/lab",
                },
                {"id": "url", "property": "search", "value": search},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[{"id": "view-store", "property": "data", "value": None}],
        )
        assert result["view-store"]["data"]["scope"]["sweeps"] == [str(SWEEP_A)]
        assert result["view-store"]["data"]["series"]["keys"] == ["loss"]
        assert result["view-store"]["data"]["active"] == "investigations"

    def test_malformed_view_parameter_defaults_and_errors(self, authed, callback_map):
        sel = encode_selection(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        search = "?sel=" + sel + "&view=%zz{"
        result = self._dispatch(
            authed,
            callback_map,
            self._HYDRATION_OUTPUTS,
            [
                {
                    "id": "url",
                    "property": "pathname",
                    "value": "/dashboard/project/lab",
                },
                {"id": "url", "property": "search", "value": search},
                {"id": "project-store", "property": "data", "value": PROJECT},
            ],
            state=[{"id": "view-store", "property": "data", "value": None}],
        )
        assert result["view-store"]["data"]["scope"]["sweeps"] == [str(SWEEP_A)]
        assert result["view-store"]["data"] == dict(
            default_view_state(),
            scope=result["view-store"]["data"]["scope"],
        )
        assert "view state is malformed" in result["analysis-message-store"]["data"]

    _SYNC_OUTPUTS = {
        "analysis-key.value",
        "analysis-mode.value",
        "analysis-reduction.value",
        "analysis-color.value",
        "analysis-facet.value",
        "analysis-display.value",
        "analysis-auto-refresh.value",
    }

    def test_control_sync_lands_each_value_on_its_component(self, authed, callback_map):
        doc = default_view_state()
        doc["series"] = {
            **doc["series"],
            "keys": ["loss"],
            "color": "shard",
            "trial_display": "median_iqr",
        }
        doc["auto_refresh"] = True
        result = self._dispatch(
            authed,
            callback_map,
            self._SYNC_OUTPUTS,
            [
                {"id": "view-store", "property": "data", "value": doc},
                {
                    "id": "analysis-key",
                    "property": "options",
                    "value": [{"value": "loss", "label": "loss"}],
                },
                {
                    "id": "analysis-color",
                    "property": "options",
                    "value": [{"value": "shard", "label": "shard"}],
                },
                {"id": "analysis-facet", "property": "options", "value": None},
            ],
        )
        assert result["analysis-key"]["value"] == ["loss"]
        assert result["analysis-mode"]["value"] == "stacked"
        assert result["analysis-reduction"]["value"] == "none"
        assert result["analysis-color"]["value"] == "shard"
        assert result["analysis-facet"]["value"] is None
        assert result["analysis-display"]["value"] == "median_iqr"
        assert result["analysis-auto-refresh"]["value"] == ["auto"]


class TestContinueInPython:
    def test_snippet_uses_real_client_api(self):
        from jernerics.tracking import TrackingClient
        from jernerics.tracking.client import ProjectHandle
        from jernerics_schema import decode_selection

        selection = Selection(project=PROJECT, sweeps=(SWEEP_A,))
        snippet = python_snippet(
            encode_selection(selection), PROJECT, "http://localhost:8000"
        )
        assert snippet.startswith("from jernerics.tracking import TrackingClient")
        assert "from jernerics_schema import decode_selection" in snippet
        assert "TrackingClient(" in snippet
        assert 'decode_selection("' in snippet
        assert f'client.project("{PROJECT}")' in snippet
        token = snippet.split('decode_selection("')[1].split('")')[0]
        assert decode_selection(token) == selection
        assert TrackingClient is not None and callable(ProjectHandle.values)

    def test_python_snippet_shows_token(self):
        snippet = python_snippet("abc123", PROJECT, "http://localhost:8000")
        assert 'decode_selection("abc123")' in snippet


class TestWorkspaceRouteServes:
    def test_page_renders_the_overview(self, service):
        page, polls = page_content("/dashboard/project/lab", service)
        assert polls is False
        rendered = str(page)
        assert "Overview" in rendered
        assert "Active sweeps" in rendered
        for label in (
            "failed executions",
            "interrupted runs",
            "completed sweeps",
            "no trials yet",
        ):
            assert label in rendered
        assert "showing 1–3 of 3" in rendered
        for name in ("alpha", "beta", "gamma"):
            assert name in rendered
        assert "/sweep/" in rendered

    def test_deep_link_with_token_returns_200(self, authed):
        token = encode_selection(Selection(project=PROJECT, sweeps=(SWEEP_A,)))
        response = authed.get(f"/dashboard/project/lab?sel={token}")
        assert response.status_code == 200
        assert "react-entry-point" in response.text


@pytest.fixture
def curated_service(tmp_path) -> tuple[Store, DashboardService]:
    store = _seeded_store(tmp_path)
    store.archive_sweep(str(SWEEP_B))
    store.mark_sweep_invalid(str(SWEEP_C), "sensor drifted after epoch 1")
    return store, DashboardService(QueryService(store))


class TestIncludeControls:
    """jernerics-cdf.2/2se: the scope group carries the discovery-only
    ``include_archived``/``include_invalid`` booleans next to the
    picked dimensions; the typed Selection keeps its token format."""

    def test_defaults_are_off_and_missing_fields_take_them(self):
        assert default_view_state()["scope"]["include_archived"] is False
        assert default_view_state()["scope"]["include_invalid"] is False
        assert decode_view_state(json.dumps({"v": VIEW_VERSION})) == (
            default_view_state()
        )

    def test_round_trip_carries_both_flags(self):
        doc = default_view_state()
        doc["scope"]["include_archived"] = True
        doc["scope"]["include_invalid"] = True
        encoded = encode_view_state(doc)
        assert decode_view_state(unquote(encoded)) == doc
        assert "include_archived" in unquote(encoded)

    @pytest.mark.parametrize(
        "payload",
        [
            json.dumps({"v": VIEW_VERSION, "scope": {"include_archived": "yes"}}),
            json.dumps({"v": VIEW_VERSION, "scope": {"include_archived": 1}}),
            json.dumps({"v": VIEW_VERSION, "scope": {"include_invalid": []}}),
        ],
    )
    def test_non_boolean_flags_fail_with_descriptive_errors(self, payload):
        with pytest.raises(ViewStateError, match="include_"):
            decode_view_state(payload)

    def test_checklist_values_and_edits_touch_only_the_flags(self):
        doc: dict[str, Any] = dict(default_view_state(), active="investigations")
        doc["series"] = {**doc["series"], "keys": ["loss"]}
        doc["scope"]["sweeps"] = [str(SWEEP_A)]
        edited = view_from_include(doc, ["invalid"])
        assert edited["scope"]["include_invalid"] is True
        assert edited["scope"]["include_archived"] is False
        assert edited["scope"]["sweeps"] == [str(SWEEP_A)]
        assert edited["series"] == doc["series"]
        assert edited["active"] == "investigations"
        assert include_values(edited) == ["invalid"]
        assert include_values(view_from_include(edited, ["archived", "invalid"])) == [
            "archived",
            "invalid",
        ]
        assert include_values(None) == []

    def test_default_flags_stay_out_of_the_url(self, service):
        assert search_from_state(default_view_state(), "") is None

    def test_include_flags_are_minted_into_the_view_parameter(self, service):
        doc = default_view_state()
        doc["scope"]["include_archived"] = True
        target = search_from_state(doc, "")
        assert target is not None and target.startswith("?view=")
        decoded = decode_view_state(unquote(target.removeprefix("?view=")))
        assert decoded["scope"]["include_archived"] is True


class TestCuratedDiscovery:
    def test_terminal_curated_sweeps_hidden_from_active_discovery(
        self, curated_service
    ):
        _store, service = curated_service
        summaries = service.sweep_overview(PROJECT)
        assert {summary.sweep_id for summary in summaries} == {
            str(SWEEP_A),
            str(SWEEP_B),
            str(SWEEP_C),
        }
        assert [summary.sweep_id for summary in active_sweeps(summaries)] == [
            str(SWEEP_A)
        ]

    def test_all_scope_keeps_distinct_curation_markers(self, curated_service):
        _store, service = curated_service
        markers = {
            summary.sweep_id: sweep_curation(summary)
            for summary in service.sweep_overview(PROJECT)
        }
        assert markers[str(SWEEP_B)] == "archived"
        assert markers[str(SWEEP_C)] == "invalid"
        assert markers[str(SWEEP_A)] == ""

    def test_hydrated_curated_token_survives_with_active_scope(self, curated_service):
        # A ?sel= token naming an invalid sweep still hydrates the scope;
        # hiding curated sweeps is a discovery-side concern, not hydration's.
        _store, service = curated_service
        selection = Selection(project=PROJECT, sweeps=(SWEEP_C,))
        tray, error = hydrate_tray(
            service,
            PROJECT,
            "/dashboard/project/lab",
            f"?sel={encode_selection(selection)}",
            None,
        )
        assert error is None and tray is not None
        assert tray["sweeps"] == [str(SWEEP_C)]


def _fake_context(*prop_ids: str):
    return type(
        "Context",
        (),
        {"triggered": [{"prop_id": prop_id, "value": None} for prop_id in prop_ids]},
    )()


class TestPatternTriggers:
    def test_pattern_trigger_resolves_metric_and_control(self):
        metric, control = pattern_trigger(
            _fake_context('{"axis-scale": "train.loss"}.value')
        )
        assert (metric, control) == ("train.loss", "axis-scale")
        assert pattern_trigger(_fake_context())[0] is None
        assert pattern_trigger(_fake_context("view-store.data"))[0] is None

    def test_move_buttons_resolve_their_direction(self):
        metric, control = pattern_trigger(
            _fake_context('{"panel-move-up": "loss"}.n_clicks')
        )
        assert (metric, control) == ("loss", "panel-move-up")


class TestAxisResolution:
    """Pure figure-layer axis rules: log refusal with counts, custom
    bounds clipping, notes."""

    def test_counts(self):
        series = [
            {"trial": str(RA0), "execution": str(EXA0), "points": [(0, 1.0), (1, 0.0)]},
            {"trial": str(RA0), "execution": None, "points": [(0, -2.0)]},
        ]
        assert non_positive_count(series) == 2
        assert clipped_count(series, -1.0, 0.5) == 2

    def test_log_refused_while_any_observation_is_non_positive(self):
        series = [
            {"trial": str(TB), "execution": str(EXB), "points": [(0, -0.5), (1, 0.25)]}
        ]
        resolved = resolve_axis({"scale": "log", "range": "auto"}, series)
        assert resolved["scale"] == "linear"
        assert resolved["log_requested"] is True
        assert resolved["non_positive"] == 1
        assert "log unavailable: 1 non-positive observation(s)" in axis_notes(resolved)

    def test_log_applied_when_all_positive(self):
        series = [{"trial": str(TB), "execution": str(EXB), "points": [(0, 0.5)]}]
        resolved = resolve_axis({"scale": "log", "range": "auto"}, series)
        assert resolved["scale"] == "log"
        assert axis_notes(resolved) == []

    def test_custom_range_reports_clipping(self):
        series = [
            {"trial": str(TB), "execution": str(EXB), "points": [(0, -0.5), (1, 0.25)]}
        ]
        resolved = resolve_axis(
            {"scale": "linear", "range": "custom", "min": -1, "max": 0}, series
        )
        assert resolved["range"] == (-1.0, 0.0)
        assert resolved["clipped"] == 1
        assert axis_notes(resolved) == [
            "log unavailable: 1 non-positive observation(s)",
            "1 observation(s) outside [-1, 0]",
        ]


class TestStackedFigureRules:
    def _per_key(self):
        shared_trial = str(RA2)
        return [
            {
                "key": "loss",
                "series": [
                    {
                        "trial": shared_trial,
                        "execution": str(EXA1),
                        "points": [(0, 0.5), (1, 0.4)],
                        "context": {"shard": 0},
                    },
                ],
            },
            {
                "key": "accuracy",
                "series": [
                    {
                        "trial": shared_trial,
                        "execution": str(EXA1),
                        "points": [(0, 0.8), (1, 0.9)],
                        "context": {"shard": 0},
                    },
                ],
            },
        ]

    def test_panels_share_x_and_keep_independent_y_axes(self):
        figure = stacked_figure(
            self._per_key(),
            {"accuracy": {"scale": "linear", "range": "custom", "min": 0, "max": 1}},
        )
        assert figure.layout.xaxis.matches == "x2"
        assert figure.layout.xaxis2.matches is None
        assert figure.layout.yaxis.range is None
        assert list(figure.layout.yaxis2.range) == [0.0, 1.0]

    def test_log_custom_bounds_render_in_log10(self):
        figure = stacked_figure(
            self._per_key(),
            {"loss": {"scale": "log", "range": "custom", "min": 1, "max": 100}},
        )
        assert figure.layout.yaxis.type == "log"
        assert list(figure.layout.yaxis.range) == [0.0, 2.0]

    def test_trial_color_stable_across_panels(self):
        figure = stacked_figure(self._per_key(), {})
        first, second = figure.data
        assert first.name == second.name == "cc310200/dd310100"
        assert first.line.color == second.line.color

    def test_facet_splits_rows_within_a_key(self):
        per_key = [
            {
                "key": "loss",
                "series": [
                    {
                        "trial": str(RA2),
                        "execution": str(EXA1),
                        "points": [(0, 0.5)],
                        "context": {"shard": 0},
                    },
                    {
                        "trial": str(RA2),
                        "execution": str(EXA2),
                        "points": [(0, 0.6)],
                        "context": {"shard": 1},
                    },
                ],
            }
        ]
        figure = stacked_figure(per_key, {}, facet="shard")
        titles = [a.text for a in figure.layout.annotations if a.text]
        assert titles == ["loss · shard = 0", "loss · shard = 1"]
        assert figure.data[0].yaxis == "y"
        assert figure.data[1].yaxis == "y2"


class TestOverlayFigureRules:
    def _per_key(self):
        return [
            {
                "key": "loss",
                "series": [
                    {
                        "trial": str(RA2),
                        "execution": str(EXA1),
                        "points": [(0, 0.5), (1, 0.4)],
                        "context": {},
                    }
                ],
            },
            {
                "key": "accuracy",
                "series": [
                    {
                        "trial": str(TB),
                        "execution": str(EXB),
                        "points": [(0, 0.81)],
                        "context": {},
                    }
                ],
            },
        ]

    def test_one_shared_y_axis_and_unnormalized_values(self):
        figure = overlay_figure(self._per_key(), None)
        yaxis_props = [key for key in figure.layout if key.startswith("yaxis")]
        assert yaxis_props == ["yaxis"]
        values = {trace.name: list(trace.y) for trace in figure.data}
        assert values["loss · cc310200/dd310100"] == [0.5, 0.4]
        assert values["accuracy · cc320000/dd320000"] == [0.81]
        assert figure.layout.yaxis.type == "linear"

    def test_pooled_log_refusal_and_shared_custom_range(self):
        per_key = [
            *self._per_key(),
            {
                "key": "delta",
                "series": [
                    {
                        "trial": str(TB),
                        "execution": str(EXB),
                        "points": [(0, -0.5), (1, 0.25)],
                        "context": {},
                    }
                ],
            },
        ]
        figure = overlay_figure(per_key, {"scale": "log", "range": "auto"})
        assert figure.layout.yaxis.type == "linear"
        figure = overlay_figure(
            per_key, {"scale": "linear", "range": "custom", "min": -1, "max": 1}
        )
        assert list(figure.layout.yaxis.range) == [-1.0, 1.0]


class TestAxisStateEdits:
    """jernerics-cdf.4 per-panel axis control edits: validation, log
    refusal with counts, reset, and the pooled overlay axis."""

    def test_custom_range_requires_both_bounds(self, service):
        doc = _series_doc(keys=["loss"])
        _panels, payload, _k, _c, _f = series_outputs(service, PROJECT, _tray(), doc)
        edited, note = axis_state_edit(
            doc,
            metric="loss",
            control="range",
            scale="linear",
            range_mode="custom",
            low=None,
            high=None,
            data=payload,
        )
        assert edited is None and note is not None
        assert "finite min and max" in note

    def test_custom_min_must_be_less_than_max(self, service):
        doc = _series_doc(keys=["loss"])
        _panels, payload, _k, _c, _f = series_outputs(service, PROJECT, _tray(), doc)
        edited, note = axis_state_edit(
            doc,
            metric="loss",
            control="max",
            scale="linear",
            range_mode="custom",
            low=0.5,
            high=0.4,
            data=payload,
        )
        assert edited is None and note is not None
        assert "min < max" in note

    def test_log_custom_bounds_must_be_positive(self):
        doc = _series_doc(
            keys=["loss"], axes={"loss": {"scale": "log", "range": "auto"}}
        )
        edited, note = axis_state_edit(
            doc,
            metric="loss",
            control="min",
            scale="log",
            range_mode="custom",
            low=-1,
            high=5,
            data=None,
        )
        assert edited is None and note is not None
        assert "min > 0" in note

    def test_log_refused_with_non_positive_count(self, service):
        doc = _series_doc(keys=["delta"])
        _panels, payload, _k, _c, _f = series_outputs(
            service, PROJECT, _all_sweeps(), doc
        )
        edited, note = axis_state_edit(
            doc,
            metric="delta",
            control="scale",
            scale="log",
            range_mode="auto",
            low=None,
            high=None,
            data=payload,
        )
        assert edited is None and note is not None
        assert "log not applied: 1 non-positive observation(s)" in note

    def test_log_accepted_for_all_positive_key(self, service):
        doc = _series_doc(keys=["loss"])
        _panels, payload, _k, _c, _f = series_outputs(service, PROJECT, _tray(), doc)
        edited, note = axis_state_edit(
            doc,
            metric="loss",
            control="scale",
            scale="log",
            range_mode="auto",
            low=None,
            high=None,
            data=payload,
        )
        assert edited is not None
        assert edited["series"]["axes"]["loss"] == {
            "scale": "log",
            "range": "auto",
            "min": None,
            "max": None,
        }
        assert note == ""

    def test_valid_custom_range_lands_and_reports_clipping(self, service):
        doc = _series_doc(keys=["loss"])
        _panels, payload, _k, _c, _f = series_outputs(service, PROJECT, _tray(), doc)
        edited, note = axis_state_edit(
            doc,
            metric="loss",
            control="max",
            scale="linear",
            range_mode="custom",
            low=0.4,
            high=0.5,
            data=payload,
        )
        assert edited is not None
        assert edited["series"]["axes"]["loss"] == {
            "scale": "linear",
            "range": "custom",
            "min": 0.4,
            "max": 0.5,
        }
        assert note is not None and "5 observation(s) outside [0.4, 0.5]" in note

    def test_auto_clears_stored_bounds_and_reset_returns_default(self, service):
        doc = _series_doc(
            keys=["loss"],
            axes={
                "loss": {
                    "scale": "log",
                    "range": "custom",
                    "min": 1.0,
                    "max": 10.0,
                }
            },
        )
        edited, _note = axis_state_edit(
            doc,
            metric="loss",
            control="range",
            scale="log",
            range_mode="auto",
            low=1,
            high=10,
            data=None,
        )
        assert edited is not None
        assert edited["series"]["axes"]["loss"] == {
            "scale": "log",
            "range": "auto",
            "min": None,
            "max": None,
        }
        reset, note = axis_state_edit(
            edited,
            metric="loss",
            control="reset",
            scale="log",
            range_mode="auto",
            low=None,
            high=None,
            data=None,
        )
        assert reset is not None
        # a fully-default axis is canonically absent, not stored
        assert reset["series"]["axes"] == {}
        assert note == ""

    def test_unchanged_edit_is_not_a_write(self):
        doc = _series_doc(keys=["loss"])
        edited, note = axis_state_edit(
            doc,
            metric="loss",
            control="scale",
            scale="linear",
            range_mode="auto",
            low=None,
            high=None,
            data=None,
        )
        assert edited is None and note is None

    def test_overlay_axis_pools_every_selected_key(self, service):
        doc = _series_doc(keys=["loss", "delta"], mode="overlay")
        _panels, payload, _k, _c, _f = series_outputs(
            service, PROJECT, _all_sweeps(), doc
        )
        edited, note = axis_state_edit(
            doc,
            metric=None,
            control="scale",
            scale="log",
            range_mode="auto",
            low=None,
            high=None,
            data=payload,
        )
        assert edited is None and note is not None
        assert "log not applied: 1 non-positive observation(s)" in note
        edited, _note = axis_state_edit(
            doc,
            metric=None,
            control="range",
            scale="linear",
            range_mode="custom",
            low=-1,
            high=2,
            data=payload,
        )
        assert edited is not None
        assert edited["series"]["overlay_axis"] == {
            "scale": "linear",
            "range": "custom",
            "min": -1.0,
            "max": 2.0,
        }
        assert edited["series"]["axes"] == {}

    def test_payload_survives_store_serialization(self, service):
        doc = _series_doc(keys=["delta"])
        _panels, payload, _k, _c, _f = series_outputs(
            service, PROJECT, _all_sweeps(), doc
        )
        round_tripped = json.loads(json.dumps(payload))
        _edited, note = axis_state_edit(
            doc,
            metric="delta",
            control="scale",
            scale="log",
            range_mode="auto",
            low=None,
            high=None,
            data=round_tripped,
        )
        assert note is not None and "1 non-positive" in note


class TestKeyOrdering:
    def test_move_up_down_and_boundaries(self):
        doc = _series_doc(keys=["loss", "accuracy", "score"])
        moved = moved_keys(doc, "accuracy", "up")
        assert moved is not None
        assert moved["series"]["keys"] == ["accuracy", "loss", "score"]
        moved = moved_keys(doc, "accuracy", "down")
        assert moved is not None
        assert moved["series"]["keys"] == ["loss", "score", "accuracy"]
        assert moved_keys(doc, "loss", "up") is None
        assert moved_keys(doc, "score", "down") is None
        assert moved_keys(doc, "ghost", "up") is None
        assert moved_keys(doc, "loss", "sideways") is None

    def test_url_round_trip_reproduces_keys_order_mode_and_axes(self, service):
        doc = _series_doc(
            keys=["delta", "loss", "accuracy"],
            mode="overlay",
            axes={
                "loss": {
                    "scale": "log",
                    "range": "custom",
                    "min": 1.0,
                    "max": 100.0,
                },
                "accuracy": {
                    "scale": "linear",
                    "range": "auto",
                    "min": None,
                    "max": None,
                },
            },
            overlay_axis={
                "scale": "linear",
                "range": "custom",
                "min": -1.0,
                "max": 2.0,
            },
        )
        search = f"?view={encode_view_state(doc)}"
        hydrated, error = hydrate_view("/dashboard/project/lab", search, None)
        assert error is None and hydrated == doc
        target = search_from_state(doc, "")
        assert target is not None and "view=" in target
        assert decode_view_state(unquote(target.split("view=")[1])) == doc

    def test_reorder_and_mode_switch_preserve_dormant_axes(self, service):
        doc = _series_doc(
            keys=["loss", "accuracy"],
            axes={
                "loss": {
                    "scale": "log",
                    "range": "custom",
                    "min": 1.0,
                    "max": 100.0,
                }
            },
        )
        reordered = moved_keys(doc, "accuracy", "up")
        assert reordered is not None
        assert reordered["series"]["keys"] == ["accuracy", "loss"]
        assert reordered["series"]["axes"] == doc["series"]["axes"]
        search = f"?view={encode_view_state(reordered)}"
        hydrated, error = hydrate_view("/dashboard/project/lab", search, None)
        assert error is None and hydrated is not None
        assert hydrated["series"]["keys"] == ["accuracy", "loss"]


SWEEP_D = uuid.UUID("aa340000-0000-4000-8000-000000000000")
TD0 = uuid.UUID("cc340000-0000-4000-8000-000000000000")
EXD0 = uuid.UUID("dd340000-0000-4000-8000-000000000000")


def _live_service(tmp_path) -> DashboardService:
    """Seeded service plus one genuinely incomplete sweep: a running
    trial with an open execution the auto-refresh transition drives to
    terminal mid-test."""
    store = _seeded_store(tmp_path)
    now = datetime.now(UTC)
    events: list[Any] = [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=now,
            project=PROJECT,
            sweep_id=SWEEP_D,
            name="delta-live",
            state="running",
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=now,
            trial_id=TD0,
            sweep_id=SWEEP_D,
            number=1,
            state=TrialState.RUNNING,
            retry_root_trial_id=TD0,
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=now,
            execution_id=EXD0,
            trial_id=TD0,
            hostname="node30",
            started_at=now,
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=now,
            trial_id=TD0,
            key="loss",
            step=0,
            value=1.1,
            context=FlatContext({"host": "node30", "shard": 0}),
        ),
    ]
    ingest = IngestService(store)
    result = ingest.apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
    )
    assert not result.conflicts
    return DashboardService(QueryService(store))


class TestTrialDisplayCodec:
    """jernerics-cdf.5: ``series.trial_display`` becomes the explicit
    all/highlighted/median_iqr enum, validated on decode."""

    def test_null_and_absent_mean_all_and_bad_values_are_rejected(self):
        assert (
            decode_view_state(json.dumps({"v": 1}))["series"]["trial_display"] == "all"
        )
        assert (
            decode_view_state(json.dumps({"v": 1, "series": {"trial_display": None}}))[
                "series"
            ]["trial_display"]
            == "all"
        )
        with pytest.raises(ViewStateError, match="unsupported trial display"):
            decode_view_state(
                json.dumps({"v": 1, "series": {"trial_display": "latest"}})
            )

    def test_filters_display_and_highlights_round_trip_the_url(self, service):
        doc = _series_doc(
            keys=["loss"],
            trial_display="median_iqr",
            context_filters={"host": ["node01"]},
        )
        doc["highlighted_trials"] = [str(RA2)]
        doc["auto_refresh"] = True
        search = f"?view={encode_view_state(doc)}"
        hydrated, error = hydrate_view("/dashboard/project/lab", search, None)
        assert error is None and hydrated == doc
        target = search_from_state(doc, "")
        assert target is not None
        decoded = decode_view_state(unquote(target.split("view=")[1]))
        assert decoded["series"]["trial_display"] == "median_iqr"
        assert decoded["series"]["context_filters"] == {"host": ["node01"]}
        assert decoded["highlighted_trials"] == [str(RA2)]
        assert decoded["auto_refresh"] is True

    def test_edits_validate_the_enum_and_preserve_dormant_filters(self):
        doc = _series_doc(
            keys=["loss"],
            trial_display="all",
            context_filters={"shard": ["0"]},
        )
        edited = view_from_controls(
            doc,
            active=None,
            keys=None,
            mode=None,
            reduction=None,
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            trial_display="garbage",
            edited={"trial_display"},
        )
        assert edited["series"]["trial_display"] == "all"
        assert edited["series"]["context_filters"] == {"shard": ["0"]}

    def test_empty_string_control_values_never_reach_the_url(self):
        """A cleared dropdown reports ""; the codec rejects empty
        strings, so the edit side must normalize to None or the next
        hydration resets the whole view to defaults."""
        doc = _series_doc(keys=["loss"], color="shard")
        doc["optuna"] = {"contour_x": "lr", "contour_y": "seed"}
        edited = view_from_controls(
            doc,
            active=None,
            keys=None,
            mode=None,
            reduction=None,
            color="",
            facet="",
            contour_x="",
            contour_y="",
            edited={"color", "facet", "contour_x", "contour_y"},
        )
        assert edited["series"]["color"] is None
        assert edited["series"]["facet"] is None
        assert edited["optuna"] == {"contour_x": None, "contour_y": None}
        decoded = decode_view_state(unquote(encode_view_state(edited)))
        assert decoded == edited

    def test_auto_refresh_edit_round_trips_through_view_from_controls(self):
        doc = default_view_state()
        edited = view_from_controls(
            doc,
            active=None,
            keys=None,
            mode=None,
            reduction=None,
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            auto_refresh=True,
            edited={"auto_refresh"},
        )
        assert edited["auto_refresh"] is True
        untouched = view_from_controls(
            edited,
            active=None,
            keys=None,
            mode=None,
            reduction=None,
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            edited=set(),
        )
        assert untouched["auto_refresh"] is True


class TestPercentileMath:
    """Pure stdlib percentile: linear interpolation on observed values
    only, with deterministic boundary behavior."""

    @pytest.mark.parametrize(
        ("values", "q", "expected"),
        [
            ([7.0], 50, 7.0),
            ([7.0], 25, 7.0),
            ([1.0, 2.0], 25, 1.25),
            ([1.0, 2.0], 50, 1.5),
            ([1.0, 2.0], 75, 1.75),
            ([1.0, 3.0, 5.0], 50, 3.0),
            ([1.0, 3.0, 5.0], 25, 2.0),
            ([5.0, 1.0, 3.0], 75, 4.0),
            ([1.0, 2.0, 3.0, 4.0], 50, 2.5),
            ([1.0, 2.0, 3.0, 4.0], 25, 1.75),
            ([2.0, 2.0, 2.0, 2.0], 75, 2.0),
            ([1.0, 2.0], 0, 1.0),
            ([1.0, 2.0], 100, 2.0),
        ],
    )
    def test_linear_interpolation_boundaries(self, values, q, expected):
        assert percentile(values, q) == pytest.approx(expected)

    def test_never_invents_points_outside_observed_values(self):
        values = [3.0, 1.0, 4.0]
        for q in (0, 10, 25, 50, 75, 90, 100):
            assert min(values) <= percentile(values, q) <= max(values)

    def test_duplicate_values_are_first_class(self):
        assert percentile([5.0, 5.0, 1.0, 5.0], 50) == pytest.approx(5.0)
        assert percentile([5.0, 5.0, 1.0, 5.0], 25) == pytest.approx(4.0)
        assert percentile([2.0, 2.0, 2.0, 2.0], 75) == pytest.approx(2.0)


class TestMedianIqrSummary:
    def _loss_per_key(self):
        return [
            {
                "key": "loss",
                "series": [
                    {
                        "trial": str(RA0),
                        "execution": str(EXA0),
                        "points": [(0, 0.9)],
                        "context": {"host": "node00", "shard": 0},
                    },
                    {
                        "trial": str(RA2),
                        "execution": str(EXA1),
                        "points": [(0, 0.5), (1, 0.4), (2, 0.3)],
                        "context": {"host": "node01", "shard": 0},
                    },
                    {
                        "trial": str(RA2),
                        "execution": str(EXA2),
                        "points": [(0, 0.6), (1, 0.5), (2, 0.4), (3, 0.35)],
                        "context": {"host": "node01", "shard": 1},
                    },
                    {
                        "trial": str(TA),
                        "execution": str(EXA3),
                        "points": [(0, 0.45), (1, 0.38)],
                        "context": {"host": "node02", "shard": 0},
                    },
                ],
            }
        ]

    def test_one_group_without_explicit_color_choices(self):
        summary = median_iqr_summary(self._loss_per_key())
        (group,) = summary["loss"]
        assert group["identity"] == "all trials"
        assert group["series_count"] == 4
        assert group["steps"] == [0, 1, 2, 3]
        assert group["median"] == pytest.approx([0.55, 0.4, 0.35, 0.35])
        assert group["q25"] == pytest.approx([0.4875, 0.39, 0.325, 0.35])
        assert group["q75"] == pytest.approx([0.675, 0.45, 0.375, 0.35])
        assert group["counts"] == [4, 3, 2, 1]

    def test_groups_only_by_explicit_color_choice(self):
        summary = median_iqr_summary(self._loss_per_key(), color="shard")
        identities = [group["identity"] for group in summary["loss"]]
        assert identities == ["0", "1"]
        by_identity = {group["identity"]: group for group in summary["loss"]}
        assert by_identity["0"]["series_count"] == 3
        assert by_identity["0"]["median"] == pytest.approx([0.5, 0.39, 0.3])
        assert by_identity["1"]["steps"] == [0, 1, 2, 3]

    def test_missing_steps_stay_missing_no_interpolation(self):
        summary = median_iqr_summary(self._loss_per_key(), color="shard")
        by_identity = {group["identity"]: group for group in summary["loss"]}
        assert by_identity["0"]["steps"] == [0, 1, 2]
        assert 3 not in by_identity["0"]["steps"]
        assert by_identity["0"]["counts"] == [3, 2, 1]

    def test_deterministic_across_calls(self):
        first = median_iqr_summary(self._loss_per_key(), color="shard")
        second = median_iqr_summary(self._loss_per_key(), color="shard")
        assert first == second


class TestDisplayModes:
    def test_all_raw_renders_every_series_and_states_the_count(self, service):
        doc = _series_doc(keys=["loss"])
        panels, _payload, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        assert len(graph.figure.data) == 4
        assert "4 series" in str(_panel_headers(panels))
        assert graph.figure.layout.uirevision == "analysis-series"

    def test_density_warning_above_100_without_sampling(self):
        dense = [
            {
                "key": "loss",
                "series": [
                    {
                        "trial": f"0000000{i:02d}-0000-4000-8000-000000000000",
                        "execution": None,
                        "points": [(0, float(i))],
                        "context": {},
                    }
                    for i in range(120)
                ],
            }
        ]
        figure = stacked_figure(dense, {}, display="all")
        assert len(figure.data) == 120
        notes = count_note(120, "all")
        assert any("line density" in note for note in notes)
        assert count_note(99, "all") == ["99 series"]
        assert count_note(120, "median_iqr") == ["120 series"]

    def test_highlighted_without_selection_shows_instruction(self, service):
        doc = _series_doc(keys=["loss"], trial_display="highlighted")
        panels, _payload, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        assert _panel_graphs(panels) == []
        assert "Highlighted only" in str(panels)
        assert "nothing is highlighted" in str(panels)

    def test_highlighted_renders_only_the_selected_identities(self, service):
        doc = _series_doc(keys=["loss"], trial_display="highlighted")
        doc["highlighted_trials"] = [str(RA2)]
        panels, _payload, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        assert {trace.name for trace in graph.figure.data} == {
            "cc310200/dd310100",
            "cc310200/dd310200",
        }
        assert {trace.customdata[0] for trace in graph.figure.data} == {str(RA2)}

    def test_all_mode_dims_everything_not_highlighted(self, service):
        doc = _series_doc(keys=["loss"])
        doc["highlighted_trials"] = [str(TA)]
        panels, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        by_name = {trace.name: trace for trace in graph.figure.data}
        assert by_name["cc310300/dd310300"].opacity is None
        assert by_name["cc310200/dd310200"].opacity == pytest.approx(0.25)

    def test_median_iqr_traces_report_counts_and_bands(self, service):
        doc = _series_doc(keys=["loss"], trial_display="median_iqr")
        panels, _payload, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        names = [trace.name for trace in graph.figure.data if trace.name]
        assert names == ["all trials · median (4 series)"]
        median_trace = next(t for t in graph.figure.data if t.name)
        assert list(median_trace.x) == [0, 1, 2, 3]
        assert list(median_trace.customdata) == [4, 3, 2, 1]
        bands = [t for t in graph.figure.data if t.fill == "tonexty"]
        assert len(bands) == 1

    def test_median_iqr_groups_only_by_explicit_color(self, service):
        doc = _series_doc(keys=["loss"], trial_display="median_iqr", color="shard")
        panels, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        names = [trace.name for trace in graph.figure.data if trace.name]
        assert names == [
            "0 · median (3 series)",
            "1 · median (1 series)",
        ]

    def test_status_line_states_both_choices(self, service):
        doc = _series_doc(keys=["loss"], trial_display="median_iqr", reduction="mean")
        _panels, payload, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        assert (
            series_status(doc, payload, incomplete=False)
            == "display: median_iqr · reduction: mean · 3 series · scope terminal"
        )

    def test_updated_ago_reads_like_every_other_page(self):
        assert updated_ago(0).startswith("Updated ")


class TestContextFilters:
    def test_service_discovers_every_dimension_value(self, service):
        dims = {
            entry["key"]: entry["values"]
            for entry in service.analysis_context_catalog(PROJECT, _tray())
        }
        assert dims["host"] == ["node00", "node01", "node02"]
        assert dims["shard"] == ["0", "1"]
        assert service.analysis_context_catalog(None, None) == []

    def test_filter_edits_add_drop_and_normalize(self):
        doc = _series_doc(keys=["loss"])
        added = view_from_context_filter(doc, "host", ["node01", "node00"])
        assert added["series"]["context_filters"] == {"host": ["node01", "node00"]}
        narrowed = view_from_context_filter(added, "host", ["node01"])
        assert narrowed["series"]["context_filters"] == {"host": ["node01"]}
        cleared = view_from_context_filter(narrowed, "host", [])
        assert cleared["series"]["context_filters"] == {}
        assert cleared["series"]["keys"] == ["loss"]

    def test_filters_apply_before_rendering_and_missing_dims_exclude(self, service):
        doc = _series_doc(keys=["loss"], context_filters={"host": ["node01"]})
        panels, payload, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        assert {trace.name for trace in graph.figure.data} == {
            "cc310200/dd310100",
            "cc310200/dd310200",
        }
        assert len(payload["per_key"]["loss"]["series"]) == 4

    def test_series_missing_the_dimension_never_matches(self):
        per_key = [
            {
                "key": "loss",
                "series": [
                    {
                        "trial": "a",
                        "execution": None,
                        "points": [(0, 1.0)],
                        "context": {},
                    },
                    {
                        "trial": "b",
                        "execution": None,
                        "points": [(0, 2.0)],
                        "context": {"host": "node01"},
                    },
                ],
            }
        ]
        filtered = apply_context_filters(per_key, {"host": ["node01"]})
        assert [series["trial"] for series in filtered[0]["series"]] == ["b"]

    def test_filter_controls_carry_all_values_and_doc_selection(self):
        dims = [
            {"key": "host", "values": ["node00", "node01"]},
            {"key": "shard", "values": ["0", "1"]},
        ]
        controls = context_filter_controls(dims, {"host": ["node01"]})
        dropdowns = [
            node
            for control in controls
            for node in _walk(control, lambda item: isinstance(item, dcc.Dropdown))
        ]
        assert [dropdown.id["context-filter"] for dropdown in dropdowns] == [
            "host",
            "shard",
        ]
        assert dropdowns[0].value == ["node01"]
        assert [option["value"] for option in dropdowns[0].options] == [
            "node00",
            "node01",
        ]
        assert dropdowns[1].value == []
        assert "No context dimensions" in str(context_filter_controls([], {}))

    def test_filters_survive_reload_through_the_url(self, service):
        doc = _series_doc(keys=["loss"], context_filters={"shard": ["0"]})
        search = f"?view={encode_view_state(doc)}"
        hydrated, error = hydrate_view("/dashboard/project/lab", search, None)
        assert error is None and hydrated is not None
        assert hydrated["series"]["context_filters"] == {"shard": ["0"]}

    def test_color_assignment_is_stable_under_filtering(self, service):
        unfiltered_doc = _series_doc(keys=["loss"])
        panels, *_rest = series_outputs(service, PROJECT, _tray(), unfiltered_doc)
        unfiltered = {
            trace.name: trace.line.color
            for trace in _panel_graphs(panels)[0].figure.data
        }
        filtered_doc = _series_doc(keys=["loss"], context_filters={"host": ["node01"]})
        panels, *_rest = series_outputs(service, PROJECT, _tray(), filtered_doc)
        for trace in _panel_graphs(panels)[0].figure.data:
            assert trace.line.color == unfiltered[trace.name]


class TestTrialBrowser:
    def test_columns_and_rows_from_existing_data(self, service):
        columns, rows, selected = browser_trial_outputs(
            service, PROJECT, _tray(sweeps=[str(SWEEP_A)]), _series_doc(keys=["loss"])
        )
        assert [column["field"] for column in columns] == [
            "swatch",
            "number",
            "trial_short",
            "state",
            "objective",
            "executions",
            "generations",
            "p_lr",
            "p_seed",
        ]
        assert columns[0]["cellClass"] == "trace-swatch-cell"
        assert columns[0]["cellStyle"] == {
            "function": "params.value ? {background: params.value} : null"
        }
        by_trial = {row["trial_short"]: row for row in rows}
        assert by_trial["cc310200"]["objective"] == "0.12"
        assert by_trial["cc310200"]["p_lr"] == "0.1"
        assert by_trial["cc310200"]["state"] == "completed"
        assert by_trial["cc310200"]["executions"] == "dd310100, dd310200"
        assert by_trial["cc310200"]["swatch"]
        assert selected == []

    def test_selection_follows_picked_family_roots(self, service):
        tray = _edit_tray([{"sweep_id": str(SWEEP_A)}], [{"root": str(RA0)}], [], None)
        _columns, rows, selected = browser_trial_outputs(
            service, PROJECT, tray, _series_doc(keys=["loss"])
        )
        assert [row["trial_id"] for row in selected] == [str(RA2)]
        assert [row["root"] for row in rows] == [str(RA0), str(TA)]

    def test_sweep_column_appears_only_for_multi_sweep_scope(self, service):
        multi = _tray(sweeps=[str(SWEEP_A), str(SWEEP_B)])
        columns, _rows, _selected = browser_trial_outputs(
            service, PROJECT, multi, _series_doc(keys=["loss"])
        )
        assert "sweep" in [column["field"] for column in columns]
        single = browser_trial_outputs(
            service, PROJECT, _tray(sweeps=[str(SWEEP_A)]), _series_doc(keys=["loss"])
        )
        assert "sweep" not in [column["field"] for column in single[0]]

    def test_swatch_matches_chart_color_for_a_sampled_param(self, service):
        doc = _series_doc(keys=["loss"], color="param:lr")
        _panels, payload, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        panels, *_ = series_outputs(service, PROJECT, _tray(), doc)
        chart = {
            trace.customdata[0]: trace.line.color
            for trace in _panel_graphs(panels)[0].figure.data
        }
        _columns, rows, _selected = browser_trial_outputs(
            service, PROJECT, _tray(), doc, payload
        )
        for row in rows:
            assert row["swatch"] == chart[row["trial_id"]]


class TestVaryingParams:
    """jernerics-igq.2: browser comparison facts name exactly the
    parameters that differ across the scoped trials."""

    def _lambda_trials(self):
        base = {
            "optimizer": "adam",
            "batch_size": 32,
            "lambda_": 0.01,
            "lambda_schedule": "constant",
            "lambda_warmup_steps": 0,
        }
        variants = [
            {"lambda_": 0.1},
            {"lambda_schedule": "cosine_ramp"},
            {"lambda_warmup_steps": 10000},
        ]
        return [
            {"trial_id": f"t{i}", "params": {**base, **delta}}
            for i, delta in enumerate([{}, *variants])
        ]

    def test_only_varying_params_become_comparison_columns(self):
        trials = self._lambda_trials()
        assert varying_param_keys(trials) == [
            "lambda_",
            "lambda_schedule",
            "lambda_warmup_steps",
        ]
        columns = browser_trial_columns(varying_param_keys(trials), multi_sweep=False)
        assert [column["field"] for column in columns][-3:] == [
            "p_lambda_",
            "p_lambda_schedule",
            "p_lambda_warmup_steps",
        ]

    def test_presence_difference_alone_varies(self):
        trials = [
            {"trial_id": "a", "params": {"x": 1}},
            {"trial_id": "b", "params": {}},
        ]
        assert varying_param_keys(trials) == ["x"]
        assert varying_param_keys([{"trial_id": "a", "params": {"x": 1}}]) == []

    def test_config_text_covers_the_varying_configuration(self):
        trials = self._lambda_trials()
        text = trial_config_text(trials[1], varying_param_keys(trials))
        assert text == "lambda_=0.1 · lambda_schedule=constant · lambda_warmup_steps=0"
        assert param_text(None) == "—"


class TestColorGrouping:
    def _records(self, values):
        return [
            {"trial": f"t{i}", "params": {"lr": value}, "context": {}}
            for i, value in enumerate(values)
        ]

    def test_categorical_and_small_numeric_are_discrete(self):
        grouping = color_grouping(self._records([0.1, 0.01, 0.1]), "param:lr")
        assert grouping["labels"] == ["0.01", "0.1"]
        assert grouping["colors"]["0.01"] != grouping["colors"]["0.1"]

    def test_numeric_over_eight_values_use_labeled_ranges(self):
        values = [round(i / 10, 1) for i in range(11)]
        grouping = color_grouping(self._records(values), "param:lr")
        assert grouping["labels"] == [
            "0-0.125",
            "0.125-0.25",
            "0.25-0.375",
            "0.375-0.5",
            "0.5-0.625",
            "0.625-0.75",
            "0.75-0.875",
            "0.875-1",
        ]
        assert identity_of(self._records([0.05])[0], grouping) == "0-0.125"
        assert identity_of(self._records([1.0])[0], grouping) == "0.875-1"

    def test_missing_values_are_gray(self):
        records = [
            {"trial": "a", "params": {"lr": 0.1}, "context": {}},
            {"trial": "b", "params": {}, "context": {}},
        ]
        grouping = color_grouping(records, "param:lr")
        assert grouping["colors"]["missing"] == "#7f7f7f"
        assert identity_of(records[1], grouping) == "missing"


class TestTraceHoverAndLegend:
    def test_hover_identifies_sweep_trial_execution_and_config(self, service):
        doc = _series_doc(keys=["loss", "accuracy"])
        panels, payload, *_rest = series_outputs(service, PROJECT, _all_sweeps(), doc)
        traces = _panel_graphs(panels)[0].figure.data
        hover = next(trace for trace in traces if trace.name == "cc310200/dd310100")
        assert "value %{y:.6g} @ step %{x}" in hover.hovertemplate
        assert "lr=" in hover.hovertemplate and "seed=" in hover.hovertemplate
        assert "cc310200" in hover.hovertemplate
        assert any(
            entry["config"]
            for entry in payload["per_key"]["loss"]["series"]
            if entry["trial"] == str(RA2)
        )

    def test_sweep_short_id_appears_in_hover_under_multi_sweep_scope(self, service):
        doc = _series_doc(keys=["loss"])
        panels, *_rest = series_outputs(service, PROJECT, _all_sweeps(), doc)
        trace = _panel_graphs(panels)[0].figure.data[0]
        assert "aa3" in trace.hovertemplate

    def test_per_trial_legend_without_a_color_choice(self, service):
        doc = _series_doc(keys=["loss"])
        panels, *_ = series_outputs(service, PROJECT, _tray(), doc)
        figure = _panel_graphs(panels)[0].figure
        names = {trace.name for trace in figure.data if trace.showlegend}
        assert names == {
            "cc310000/dd310000",
            "cc310200/dd310100",
            "cc310300/dd310300",
        }
        assert figure.layout.showlegend is True

    def test_one_row_stacked_height_is_450_px(self, service):
        doc = _series_doc(keys=["loss"])
        panels, *_ = series_outputs(service, PROJECT, _tray(), doc)
        assert _panel_graphs(panels)[0].figure.layout.height == 450

    def test_plot_click_sets_focus_and_toggles_highlight(self):
        doc = _series_doc(keys=["loss"])
        click = {"points": [{"customdata": str(RA2)}]}
        picked = view_from_trace_click(doc, click)
        assert picked is not None
        assert picked["highlighted_trials"] == [str(RA2)]
        assert picked["focus"] == {"kind": "trial", "id": str(RA2)}
        cleared = view_from_trace_click(picked, click)
        assert cleared is not None
        assert cleared["highlighted_trials"] == []
        assert cleared["focus"] == {"kind": "trial", "id": str(RA2)}
        assert view_from_trace_click(doc, {"points": [{}]}) is None
        assert view_from_trace_click(doc, None) is None
        assert picked["series"]["keys"] == ["loss"]

    def test_traces_carry_the_trial_identity_for_plot_clicks(self, service):
        doc = _series_doc(keys=["loss"])
        panels, *_rest = series_outputs(service, PROJECT, _tray(), doc)
        graph = _panel_graphs(panels)[0]
        assert {trace.customdata[0] for trace in graph.figure.data} == {
            str(RA0),
            str(RA2),
            str(TA),
        }


class TestRefreshBehavior:
    def test_scope_incompleteness_follows_running_work(self, tmp_path):
        service = _live_service(tmp_path)
        assert service.analysis_scope_incomplete(PROJECT, _tray(sweeps=[str(SWEEP_D)]))
        assert not service.analysis_scope_incomplete(
            PROJECT, _tray(sweeps=[str(SWEEP_A)])
        )

    def test_auto_refresh_polls_gates_on_intent_and_incompleteness(self, tmp_path):
        service = _live_service(tmp_path)
        live_doc = edited_view(
            default_view_state(),
            {"auto_refresh": True, "scope": _tray(sweeps=[str(SWEEP_D)])},
        )
        done_doc = edited_view(
            default_view_state(),
            {"auto_refresh": True, "scope": _tray(sweeps=[str(SWEEP_A)])},
        )
        assert auto_refresh_polls(service, PROJECT, live_doc)
        assert not auto_refresh_polls(service, PROJECT, done_doc)
        assert not auto_refresh_polls(service, PROJECT, default_view_state())
        assert not auto_refresh_polls(None, None, None)

    def test_page_content_polls_while_visible_sweeps_are_incomplete(self, tmp_path):
        service = _live_service(tmp_path)
        _page, polls = page_content("/dashboard/project/lab", service)
        assert polls is True

    def test_auto_refresh_flips_off_only_when_terminal(self):
        doc = dict(default_view_state(), auto_refresh=True)
        assert auto_refresh_flip(doc, True) is None
        flipped = auto_refresh_flip(doc, False)
        assert flipped is not None and flipped["auto_refresh"] is False
        assert auto_refresh_flip(default_view_state(), False) is None

    def test_series_data_and_view_outputs_shape(self, tmp_path):
        service = _live_service(tmp_path)
        doc = _series_doc(keys=["loss"], context_filters={"host": ["node01"]})
        snapshot, updated, refresh = series_data_outputs(
            service, PROJECT, _tray(sweeps=[str(SWEEP_A)]), doc, 1_000
        )
        assert updated.startswith("Updated ")
        assert refresh == {"error": "", "at_ns": 1_000}
        assert snapshot["reduction"] == "none"
        assert snapshot["fingerprint"] == scope_fingerprint(
            PROJECT, _tray(sweeps=[str(SWEEP_A)])
        )
        assert set(snapshot["per_key"]) == {"loss"}
        assert snapshot["dims"] and snapshot["key_options"]
        (
            panels,
            persist,
            key_options,
            color_options,
            facet_options,
            filters,
            status,
            figure,
        ) = series_view_outputs(
            service,
            PROJECT,
            _tray(sweeps=[str(SWEEP_A)]),
            doc,
            snapshot,
            1_000,
        )
        assert _panel_graphs(panels) == []
        assert figure is not None and figure.layout.uirevision == "analysis-series"
        assert persist is no_update
        assert key_options
        assert [option["value"] for option in facet_options] == ["host", "shard"]
        assert color_options
        dropdowns = [
            node
            for control in filters
            for node in _walk(control, lambda item: isinstance(item, dcc.Dropdown))
        ]
        assert [dropdown.id["context-filter"] for dropdown in dropdowns] == [
            "host",
            "shard",
        ]
        assert status.startswith("display: all · reduction: none · ")

    def test_series_data_failure_keeps_last_success_and_surfaces_error(self):
        failure = series_data_failure(RuntimeError("store is gone"), 42)
        assert failure[0] is no_update
        assert failure[1] is no_update
        assert failure[2] == {
            "error": "refresh failed — keeping the last successful view: store is gone",
            "at_ns": 42,
        }

    def test_broken_service_read_raises_through_to_the_callback_wrap(self):
        class BrokenService:
            def analysis_value_keys(self, *args, **kwargs):
                raise RuntimeError("boom")

        broken: Any = BrokenService()
        with pytest.raises(RuntimeError, match="boom"):
            series_snapshot(broken, PROJECT, _tray(), None, 1)


class TestSeriesSnapshotQueryCounts:
    """jernerics-igq.3 query-count contract: view-only edits, removals,
    and reorders reuse the stored snapshot with zero reads; additions
    read only the missing keys; reduction changes rebuild."""

    _READS = (
        "values",
        "value_key_coverage",
        "context_catalog",
        "trial_numbers_objectives",
    )

    @staticmethod
    def _counting(service, monkeypatch):
        from collections import Counter

        counts = Counter()
        for name in TestSeriesSnapshotQueryCounts._READS:
            original = getattr(service.queries, name)

            def spy(*args, _name=name, _original=original, **kwargs):
                counts[_name] += 1
                return _original(*args, **kwargs)

            monkeypatch.setattr(service.queries, name, spy)
        return counts

    def test_view_only_edits_and_removal_read_nothing(self, service, monkeypatch):
        from collections import Counter

        counts = self._counting(service, monkeypatch)
        tray = _tray()
        doc = _series_doc(keys=["loss", "accuracy"])
        snapshot = series_snapshot(service, PROJECT, tray, doc, 0)
        baseline = Counter(counts)
        assert baseline["values"] == 1
        assert baseline["value_key_coverage"] == 1
        assert baseline["context_catalog"] == 1
        assert baseline["trial_numbers_objectives"] == 1
        view_edits = [
            _series_doc(keys=["loss", "accuracy"], mode="overlay"),
            _series_doc(keys=["loss", "accuracy"], trial_display="median_iqr"),
            _series_doc(keys=["loss", "accuracy"], color="shard"),
            _series_doc(keys=["loss", "accuracy"], facet="host"),
            _series_doc(
                keys=["loss", "accuracy"], context_filters={"host": ["node01"]}
            ),
            {
                **_series_doc(keys=["loss", "accuracy"]),
                "series": {
                    **_series_doc(keys=["loss", "accuracy"])["series"],
                    "axes": {
                        "loss": {
                            "scale": "log",
                            "range": "auto",
                            "min": None,
                            "max": None,
                        }
                    },
                },
            },
            {
                **_series_doc(keys=["loss", "accuracy"]),
                "highlighted_trials": [str(RA2)],
            },
            _series_doc(keys=["accuracy", "loss"]),
            _series_doc(keys=["loss"]),
            _series_doc(keys=[]),
        ]
        for edited in view_edits:
            outputs = series_view_outputs(service, PROJECT, tray, edited, snapshot, 1)
            assert outputs[1] is no_update, edited
        assert counts == baseline

    def test_adding_one_key_reads_only_that_key(self, service, monkeypatch):
        from collections import Counter

        counts = self._counting(service, monkeypatch)
        tray = _tray()
        snapshot = series_snapshot(
            service, PROJECT, tray, _series_doc(keys=["loss"]), 0
        )
        counts.clear()
        outputs = series_view_outputs(
            service, PROJECT, tray, _series_doc(keys=["loss", "accuracy"]), snapshot, 1
        )
        assert counts == Counter({"values": 1})
        merged = outputs[1]
        assert merged is not no_update
        assert set(merged["per_key"]) == {"loss", "accuracy"}
        assert merged["fingerprint"] == snapshot["fingerprint"]
        counts.clear()
        again = series_view_outputs(
            service, PROJECT, tray, _series_doc(keys=["loss", "accuracy"]), merged, 2
        )
        assert counts == Counter()
        assert again[1] is no_update

    def test_reduction_change_rebuilds_the_snapshot(self, service, monkeypatch):
        from collections import Counter

        counts = self._counting(service, monkeypatch)
        tray = _tray()
        snapshot = series_snapshot(
            service, PROJECT, tray, _series_doc(keys=["loss"]), 0
        )
        counts.clear()
        outputs = series_view_outputs(
            service,
            PROJECT,
            tray,
            _series_doc(keys=["loss"], reduction="mean"),
            snapshot,
            1,
        )
        assert counts == Counter(
            {
                "values": 1,
                "value_key_coverage": 1,
                "context_catalog": 1,
                "trial_numbers_objectives": 1,
            }
        )
        assert outputs[1] is not no_update
        assert outputs[1]["reduction"] == "mean"


class TestContextDiscoveryBeyondPagination:
    """>100 pages of stored values must not page through tracked_values
    to discover context filter options."""

    @staticmethod
    def _big_store(tmp_path):
        store = Store(tmp_path / "big.sqlite")
        now = datetime.now(UTC)
        sweep = uuid.uuid4()
        trial = uuid.uuid4()
        execution = uuid.uuid4()
        ingest = IngestService(store)
        head: list[TrackingEvent] = [
            SweepSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=now,
                project=PROJECT,
                sweep_id=sweep,
                name="big",
                state="completed",
            ),
            TrialSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=now,
                trial_id=trial,
                sweep_id=sweep,
                number=1,
                state=TrialState.COMPLETED,
                retry_root_trial_id=trial,
            ),
            ExecutionStartEvent(
                event_id=uuid.uuid4(),
                recorded_at=now,
                execution_id=execution,
                trial_id=trial,
                hostname="node00",
                started_at=now,
            ),
        ]
        result = ingest.apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=head)
        )
        assert not result.conflicts
        chunk = 100
        for start in range(0, 101_000, chunk):
            events: list[TrackingEvent] = [
                ValueEvent(
                    event_id=uuid.uuid4(),
                    recorded_at=now,
                    trial_id=trial,
                    execution_id=execution,
                    key="loss",
                    step=step,
                    value=float(step),
                    context=FlatContext({"host": "node00", "shard": step % 2}),
                )
                for step in range(start, min(start + chunk, 101_000))
            ]
            result = ingest.apply(
                IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
            )
            assert not result.conflicts
        return store

    def test_project_wide_discovery_skips_value_pages(self, tmp_path, monkeypatch):
        store = self._big_store(tmp_path)
        queries = QueryService(store)
        service = DashboardService(queries)
        monkeypatch.setattr(
            queries, "values", lambda *a, **k: pytest.fail("paginated values read")
        )
        dims = {
            entry["key"]: entry["values"]
            for entry in service.analysis_context_catalog(PROJECT, None)
        }
        assert dims["host"] == ["node00"]
        assert dims["shard"] == ["0", "1"]


class TestWorkspaceChurnGates:
    """jernerics-igq.3: the project picker ignores URL view edits once a
    project is established, and the scroll capture/restore clientside
    callbacks stay wired."""

    @pytest.fixture(scope="class")
    def dash_app(self, tmp_path_factory):
        service = DashboardService(
            QueryService(_seeded_store(tmp_path_factory.mktemp("churn-gates")))
        )
        ctx = DashboardContext(
            api_key=API_KEY,
            queries=service.queries,
            service=service,
            signer=SessionSigner(b"\x00" * 32),
        )
        return build_dash_app(ctx)

    @staticmethod
    def _callback_key(callback_map, wanted: set[str]) -> str:
        def outputs_of(key):
            stripped = key.removeprefix("..").removesuffix("..")
            return {part.split("@")[0] for part in stripped.split("...") if part}

        return next(key for key in callback_map if outputs_of(key) == wanted)

    def _post(self, client, callback_map, wanted, inputs, state=(), changed=()):
        key = self._callback_key(callback_map, wanted)
        specs = [
            part.split("@")[0]
            for part in key.removeprefix("..").removesuffix("..").split("...")
            if part
        ]
        outputs = [
            {"id": spec.split(".")[0], "property": spec.split(".")[1]} for spec in specs
        ]
        return client.post(
            "/dashboard/_dash-update-component",
            json={
                "output": key,
                "outputs": outputs[0] if len(outputs) == 1 else outputs,
                "inputs": inputs,
                "state": list(state),
                "changedPropIds": list(changed),
            },
        )

    def test_picker_ignores_url_view_edits_once_a_project_is_established(
        self, authed, dash_app
    ):
        view_url = f"?view={encode_view_state(default_view_state())}"
        cascade = self._post(
            authed,
            dash_app.callback_map,
            {"project-picker.value"},
            [
                {"id": "project-store", "property": "data", "value": PROJECT},
                {"id": "url", "property": "search", "value": view_url},
            ],
            state=[{"id": "project-picker", "property": "value", "value": None}],
            changed=["url.search"],
        )
        assert cascade.status_code == 204
        settle = self._post(
            authed,
            dash_app.callback_map,
            {"project-picker.value"},
            [
                {"id": "project-store", "property": "data", "value": PROJECT},
                {"id": "url", "property": "search", "value": view_url},
            ],
            state=[{"id": "project-picker", "property": "value", "value": None}],
            changed=["project-store.data"],
        )
        assert settle.status_code == 200
        assert settle.json()["response"]["project-picker"]["value"] == PROJECT

    def test_scroll_capture_and_restore_callbacks_are_wired(self, dash_app):
        def specs(output_id):
            found = []
            for key, spec in dash_app.callback_map.items():
                outputs = {
                    part.split("@")[0]
                    for part in key.removeprefix("..").removesuffix("..").split("...")
                    if part
                }
                if outputs == {f"{output_id}.data"}:
                    found.append(spec)
            return found

        capture = specs("scroll-restore-store")

        def input_ids(spec):
            return {
                dep["id"]
                if isinstance(dep["id"], str)
                else json.dumps(dep["id"], sort_keys=True)
                for dep in spec["inputs"]
            }

        assert any(
            input_ids(spec) == {'{"analysis-refresh":["ALL"]}', "poll"}
            for spec in capture
        )
        restore = [
            spec
            for spec in capture
            if input_ids(spec) == {'{"analysis-refresh-store":["ALL"]}'}
        ]
        assert len(restore) == 1
        assert {dep["id"] for dep in restore[0].get("state", [])} == {
            "scroll-restore-store"
        }
