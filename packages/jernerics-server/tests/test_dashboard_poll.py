import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionEndEvent,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    Selection,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    encode_selection,
)
from jernerics_server.dashboard.analysis import (
    default_scope_state,
    default_view_state,
    encode_view_state,
    expand_values,
    include_values,
)
from jernerics_server.dashboard.app import build_dash_app
from jernerics_server.dashboard.auth import DashboardContext
from jernerics_server.dashboard.service import DashboardService
from jernerics_server.dashboard.sessions import SessionSigner
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store

API_KEY = "secret123"
PROJECT = "lab"
WORKSPACE = "/dashboard/project/lab"

SWEEP_A = uuid.UUID("aa100000-0000-4000-8000-000000000000")
SWEEP_B = uuid.UUID("aa110000-0000-4000-8000-000000000000")
ROOT_A = uuid.UUID("cc200000-0000-4000-8000-000000000000")
ROOT_B = uuid.UUID("cc210000-0000-4000-8000-000000000000")
TRIAL_A = uuid.UUID("cc100000-0000-4000-8000-000000000000")
EXEC_A = uuid.UUID("dd100000-0000-4000-8000-000000000000")

_BASE = datetime(2025, 3, 1, 12, 0, 0, tzinfo=UTC)


def _event(cls, seconds_ago: float, **kwargs):
    return cls(
        event_id=uuid.uuid4(),
        recorded_at=_BASE - timedelta(seconds=seconds_ago),
        **kwargs,
    )


def _seed_events() -> list:
    return [
        _event(
            SweepSnapshotEvent,
            1000,
            project=PROJECT,
            sweep_id=SWEEP_A,
            name="alpha",
            state="completed",
        ),
        _event(
            TrialSnapshotEvent,
            990,
            trial_id=TRIAL_A,
            sweep_id=SWEEP_A,
            number=1,
            state=TrialState.COMPLETED,
            retry_root_trial_id=TRIAL_A,
            objective=0.5,
            params=FlatContext({"lr": 0.1}),
        ),
        _event(
            ExecutionStartEvent,
            985,
            execution_id=EXEC_A,
            trial_id=TRIAL_A,
            hostname="node00",
            started_at=_BASE - timedelta(seconds=985),
        ),
        _event(ValueEvent, 980, trial_id=TRIAL_A, key="loss", step=0, value=0.9),
        _event(
            ExecutionEndEvent,
            975,
            execution_id=EXEC_A,
            ended_at=_BASE - timedelta(seconds=975),
            outcome="success",
            exit_code=0,
        ),
    ]


def _seeded_store(path) -> Store:
    store = Store(path / "poll.sqlite")
    result = IngestService(store).apply(
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=_seed_events())
    )
    assert not result.conflicts
    return store


def _ingest_sweep(store: Store, sweep_id: uuid.UUID, name: str) -> None:
    result = IngestService(store).apply(
        IngestRequest(
            protocol_version=PROTOCOL_VERSION,
            events=[
                _event(
                    SweepSnapshotEvent,
                    500,
                    project=PROJECT,
                    sweep_id=sweep_id,
                    name=name,
                    state="completed",
                )
            ],
        )
    )
    assert not result.conflicts


@pytest.fixture
def authed(tmp_path) -> tuple[TestClient, Store]:
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
        QueryService(_seeded_store(tmp_path_factory.mktemp("poll-graph")))
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


def _callback_key(callback_map, wanted: set[str], input_ids: set[str]) -> str:
    """The callback writing exactly ``wanted`` from ``input_ids`` inputs;
    single-output store writers collide on outputs alone."""
    return next(
        key
        for key in callback_map
        if _outputs_of(key) == wanted
        and {spec["id"] for spec in callback_map[key]["inputs"]} == input_ids
    )


def _dispatch(
    client, callback_map, wanted: set[str], inputs, state=(), changed=None, key=None
):
    key = key or next(k for k in callback_map if _outputs_of(k) == wanted)
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
            "inputs": list(inputs),
            "state": list(state),
            "changedPropIds": changed
            or [f"{item['id']}.{item['property']}" for item in inputs],
        },
    )
    payload = response.json()["response"] if response.status_code == 200 else None
    return response, payload


_VIEW_INPUT = {"id": "view-store", "property": "data", "value": None}
_TICK_INPUT = {"id": "poll", "property": "n_intervals", "value": 1}
_TAB_INPUT = {"id": "analysis-tabs", "property": "value", "value": "overview"}
_PROJECT_STATE = {"id": "project-store", "property": "data", "value": PROJECT}


def _digest_state(doc):
    return {"id": "overview-digest-store", "property": "data", "value": doc}


_OVERVIEW_OUTPUTS = {"workspace-overview.children"}
_TRACKER_OUTPUTS = {"overview-digest-store.data"}


class TestOverviewPollCascade:
    """jernerics-haj: the overview region must stop re-shipping its 9KB
    tree on every poll tick when the server data did not change."""

    def _fire(self, client, cmap, outputs, digest_doc, changed):
        return _dispatch(
            client,
            cmap,
            outputs,
            inputs=[_VIEW_INPUT, _TICK_INPUT, _TAB_INPUT],
            state=[_PROJECT_STATE, _digest_state(digest_doc)],
            changed=changed,
        )

    def test_unchanged_tick_ships_nothing(self, authed, callback_map):
        client, _store = authed
        response, tracked = self._fire(
            client, callback_map, _TRACKER_OUTPUTS, None, ["view-store.data"]
        )
        assert response.status_code == 200
        digest = tracked["overview-digest-store"]["data"]["digest"]
        response, rendered = self._fire(
            client, callback_map, _OVERVIEW_OUTPUTS, None, ["view-store.data"]
        )
        assert response.status_code == 200
        assert rendered["workspace-overview"]["children"]
        # the next tick, with nothing changed server-side, ships no tree
        for outputs in (_TRACKER_OUTPUTS, _OVERVIEW_OUTPUTS):
            tick = self._fire(
                client, callback_map, outputs, {"digest": digest}, ["poll.n_intervals"]
            )
            assert tick[0].status_code == 204

    def test_changed_data_advances_digest_and_re_renders(self, authed, callback_map):
        client, store = authed
        _response, tracked = self._fire(
            client, callback_map, _TRACKER_OUTPUTS, None, ["view-store.data"]
        )
        stale = tracked["overview-digest-store"]["data"]
        _ingest_sweep(store, SWEEP_B, "beta")
        _response, tracked = self._fire(
            client,
            callback_map,
            _TRACKER_OUTPUTS,
            stale,
            ["poll.n_intervals"],
        )
        fresh = tracked["overview-digest-store"]["data"]
        response, _rendered = self._fire(
            client, callback_map, _OVERVIEW_OUTPUTS, stale, ["poll.n_intervals"]
        )
        assert response.status_code == 200
        assert "beta" in response.text

    def test_page_render_voids_digest_but_a_tick_does_not(self, authed, callback_map):
        client, _store = authed
        wanted = {
            "page-container.children",
            "poll.disabled",
            "view-store.data",
            "route-store.data",
            "overview-digest-store.data",
        }
        inputs = [
            {"id": "url", "property": "pathname", "value": WORKSPACE},
            {"id": "poll", "property": "n_intervals", "value": 0},
        ]
        response, page = _dispatch(
            client,
            callback_map,
            wanted,
            inputs=inputs,
            state=[
                {"id": "workspace-store", "property": "data", "value": None},
                {"id": "view-store", "property": "data", "value": None},
                {"id": "route-store", "property": "data", "value": None},
            ],
            changed=["url.pathname"],
        )
        assert response.status_code == 200
        assert page["overview-digest-store"]["data"] is None
        assert page["route-store"]["data"] == WORKSPACE
        _response, tick = _dispatch(
            client,
            callback_map,
            wanted,
            inputs=[
                {"id": "url", "property": "pathname", "value": WORKSPACE},
                {"id": "poll", "property": "n_intervals", "value": 1},
            ],
            state=[
                {"id": "workspace-store", "property": "data", "value": None},
                {"id": "view-store", "property": "data", "value": None},
                {"id": "route-store", "property": "data", "value": WORKSPACE},
            ],
            changed=["poll.n_intervals"],
        )
        # a tick never touches the page, the digest, or the route
        assert set(tick) == {"poll"}


class TestPickerNavigationMirrorGuard:
    """The picker's mirror write on load must not rewrite the pathname
    the router already rendered — that rewrite re-fires every
    pathname-driven callback for a second hydration lap. A genuine
    project switch also clears the stale ``?view=`` so the previous
    project's scope cannot ride along (jernerics-2se)."""

    def _navigate(self, client, callback_map, picked, rendered_route):
        return _dispatch(
            client,
            callback_map,
            {"url.pathname", "url.search"},
            inputs=[{"id": "project-picker", "property": "value", "value": picked}],
            state=[{"id": "route-store", "property": "data", "value": rendered_route}],
        )

    def test_matching_route_is_not_rewritten(self, authed, callback_map):
        client, _store = authed
        response, _payload = self._navigate(client, callback_map, PROJECT, WORKSPACE)
        assert response.status_code == 204

    def test_new_project_still_navigates_and_clears_the_scope_url(
        self, authed, callback_map
    ):
        client, _store = authed
        response, payload = self._navigate(client, callback_map, PROJECT, "/dashboard/")
        assert response.status_code == 200
        assert payload["url"]["pathname"] == WORKSPACE

    def test_project_switch_clears_a_stale_view_parameter(self, authed, callback_map):
        client, _store = authed
        rendered = f"{WORKSPACE}?view=%7B%22v%22%3A2%7D"
        response, payload = self._navigate(client, callback_map, "other", rendered)
        assert response.status_code == 200
        assert payload["url"]["search"] == ""

    def test_cleared_picker_returns_to_catalog(self, authed, callback_map):
        client, _store = authed
        response, payload = self._navigate(client, callback_map, None, WORKSPACE)
        assert response.status_code == 200
        assert payload["url"]["pathname"] == "/dashboard/"


class TestSweepsBrowserTickGuard:
    """The sweeps loader's digest guard (unchanged rows on a poll tick
    ship nothing) pinned so it cannot regress silently. jernerics-2se:
    the scope now lives in the view doc — an unchanged scope on a tick
    must ship nothing, and an unrelated view edit (focus) must not
    re-ship the grid either."""

    _WANTED = {
        "sweep-grid.rowData",
        "sweep-grid.selectedRows",
        "workspace-curation-note.children",
        "sweep-browser-facts-store.data",
    }

    def _fire(self, client, callback_map, view_doc, facts, changed):
        return _dispatch(
            client,
            callback_map,
            self._WANTED,
            inputs=[
                _PROJECT_STATE,
                {"id": "view-store", "property": "data", "value": view_doc},
                _TICK_INPUT,
            ],
            state=[
                {"id": "sweep-browser-facts-store", "property": "data", "value": facts}
            ],
            changed=changed,
        )

    def test_unchanged_rows_tick_is_skipped(self, authed, callback_map):
        client, _store = authed
        response, loaded = self._fire(client, callback_map, None, None, None)
        assert response.status_code == 200
        facts = loaded["sweep-browser-facts-store"]["data"]
        assert facts["digest"]
        tick = self._fire(
            client,
            callback_map,
            None,
            facts,
            ["poll.n_intervals"],
        )
        assert tick[0].status_code == 204

    def test_unchanged_scope_on_a_tick_ships_nothing(self, authed, callback_map):
        """jernerics-2se regression: the sweep picks hydrate into the
        view doc; a poll tick carrying the same scope ships nothing."""
        client, _store = authed
        sweep_id = str(SWEEP_A)
        doc = default_view_state()
        doc["scope"]["sweeps"] = [sweep_id]
        response, loaded = self._fire(
            client, callback_map, doc, None, ["view-store.data"]
        )
        assert response.status_code == 200
        facts = loaded["sweep-browser-facts-store"]["data"]
        assert facts["sweeps"] == [sweep_id]
        picked = [row["sweep_id"] for row in loaded["sweep-grid"]["selectedRows"]]
        assert picked == [sweep_id]
        # a tick re-fires with the identical doc: the digest guard skips
        tick = self._fire(client, callback_map, doc, facts, ["poll.n_intervals"])
        assert tick[0].status_code == 204
        # an identical scope re-write through the store is skipped too
        again = self._fire(client, callback_map, doc, facts, ["view-store.data"])
        assert again[0].status_code == 204


class TestInspectorPollCascade:
    """The per-tick inspector re-render rebuilt the focus controls,
    re-firing the focus editor (the double view-store writer) on every
    tick; identical content must ship nothing."""

    def _fire(self, client, cmap, view_doc, rendered):
        return _dispatch(
            client,
            cmap,
            {"inspector.children", "inspector-render-store.data"},
            inputs=[
                {"id": "view-store", "property": "data", "value": view_doc},
                _TICK_INPUT,
            ],
            state=[
                _PROJECT_STATE,
                {"id": "inspector-render-store", "property": "data", "value": rendered},
            ],
        )

    def test_focused_renders_then_static_tick_ships_nothing(self, authed, callback_map):
        client, _store = authed
        focused = {"focus": {"kind": "sweep", "id": str(SWEEP_A)}}
        response, payload = self._fire(client, callback_map, focused, None)
        assert response.status_code == 200
        assert "alpha" in response.text
        rendered = payload["inspector-render-store"]["data"]
        assert rendered["focus"] == focused["focus"]
        tick = self._fire(client, callback_map, focused, rendered)
        assert tick[0].status_code == 204

    def test_unfocused_placeholder_tick_ships_nothing(self, authed, callback_map):
        client, _store = authed
        response, payload = self._fire(client, callback_map, None, None)
        assert response.status_code == 200
        rendered = payload["inspector-render-store"]["data"]
        tick = self._fire(client, callback_map, None, rendered)
        assert tick[0].status_code == 204


_HYDRATION_OUTPUTS = {
    "analysis-message-store.data",
    "view-store.data",
}
_TRAY_EDIT_OUTPUTS = {"view-store.data"}
_INCLUDE_EDIT_OUTPUTS = {"view-store.data"}
_MERGED_SYNC_OUTPUTS = {
    "analysis-tabs.value",
    "analysis-key.value",
    "analysis-mode.value",
    "analysis-reduction.value",
    "analysis-color.value",
    "analysis-facet.value",
    "analysis-contour-x.value",
    "analysis-contour-y.value",
    "analysis-display.value",
    "analysis-auto-refresh.value",
    "analysis-include.value",
    "analysis-expand.value",
}


class TestHydrationCanonicalEcho:
    """jernerics-haj: hydration writes the canonical view doc, so the
    control-sync -> edit-callback echo finds the store already in the
    form it would compute and ships nothing — the load cascade's lap-2
    store writes and the region re-renders they drag along."""

    def _hydrate(self, client, cmap, search, view):
        return _dispatch(
            client,
            cmap,
            _HYDRATION_OUTPUTS,
            inputs=[
                {"id": "url", "property": "pathname", "value": WORKSPACE},
                {"id": "url", "property": "search", "value": search},
                _PROJECT_STATE,
            ],
            state=[{"id": "view-store", "property": "data", "value": view}],
            changed=["url.search"],
        )

    def test_tray_echo_after_hydration_ships_nothing(self, authed, callback_map):
        client, _store = authed
        token = encode_selection(
            Selection(
                project=PROJECT,
                sweeps=(SWEEP_B, SWEEP_A, SWEEP_B),
                retry_roots=(ROOT_B, ROOT_A, ROOT_B),
            )
        )
        response, payload = self._hydrate(
            client, callback_map, f"?sel={token}", default_view_state()
        )
        assert response.status_code == 200
        doc = payload["view-store"]["data"]
        assert doc["scope"]["sweeps"] == sorted({str(SWEEP_A), str(SWEEP_B)})
        assert doc["scope"]["families"] == sorted({str(ROOT_A), str(ROOT_B)})
        echo = _dispatch(
            client,
            callback_map,
            _TRAY_EDIT_OUTPUTS,
            inputs=[
                {
                    "id": "analysis-family-grid",
                    "property": "selectedRows",
                    "value": [{"root": str(ROOT_B)}, {"root": str(ROOT_A)}],
                },
                {
                    "id": "analysis-expand",
                    "property": "value",
                    "value": expand_values(doc),
                },
            ],
            state=[{"id": "view-store", "property": "data", "value": doc}],
            changed=["analysis-family-grid.selectedRows", "analysis-expand.value"],
            key=_callback_key(
                callback_map,
                _TRAY_EDIT_OUTPUTS,
                {"analysis-family-grid", "analysis-expand"},
            ),
        )
        assert echo[0].status_code == 204

    def test_include_echo_after_hydration_ships_nothing(self, authed, callback_map):
        client, _store = authed
        doc = default_view_state()
        doc["scope"]["include_archived"] = True
        doc["scope"]["include_invalid"] = True
        response, payload = self._hydrate(
            client, callback_map, f"?view={encode_view_state(doc)}", None
        )
        assert response.status_code == 200
        assert payload["view-store"]["data"] == doc
        echo = _dispatch(
            client,
            callback_map,
            _INCLUDE_EDIT_OUTPUTS,
            inputs=[
                {
                    "id": "analysis-include",
                    "property": "value",
                    "value": include_values(doc),
                }
            ],
            state=[{"id": "view-store", "property": "data", "value": doc}],
            changed=["analysis-include.value"],
            key=_callback_key(
                callback_map, _INCLUDE_EDIT_OUTPUTS, {"analysis-include"}
            ),
        )
        assert echo[0].status_code == 204

    def test_rehydrating_the_hydrated_scope_rewrites_nothing(
        self, authed, callback_map
    ):
        client, _store = authed
        token = encode_selection(
            Selection(project=PROJECT, retry_roots=(ROOT_A, ROOT_B))
        )
        _response, payload = self._hydrate(client, callback_map, f"?sel={token}", None)
        doc = payload["view-store"]["data"]
        again, payload = self._hydrate(client, callback_map, f"?sel={token}", doc)
        assert again.status_code == 200
        assert "view-store" not in payload


class TestMergedControlSync:
    """The three control syncs merged into one callback: one POST per
    store change, producing the include/expand/tab values the separate
    syncs wrote for the same store states."""

    def _sync(self, client, cmap, doc, key_options=None):
        return _dispatch(
            client,
            cmap,
            _MERGED_SYNC_OUTPUTS,
            inputs=[
                {"id": "view-store", "property": "data", "value": doc},
                {"id": "analysis-key", "property": "options", "value": key_options},
                {"id": "analysis-color", "property": "options", "value": None},
                {"id": "analysis-facet", "property": "options", "value": None},
                {"id": "analysis-contour-x", "property": "options", "value": None},
                {"id": "analysis-contour-y", "property": "options", "value": None},
            ],
            changed=["view-store.data"],
        )

    def test_defaults_match_the_separate_syncs(self, authed, callback_map):
        client, _store = authed
        response, payload = self._sync(client, callback_map, None)
        assert response.status_code == 200
        assert payload["analysis-tabs"]["value"] == "overview"
        assert payload["analysis-key"]["value"] == []
        assert payload["analysis-mode"]["value"] == "stacked"
        assert payload["analysis-reduction"]["value"] == "none"
        assert payload["analysis-color"]["value"] is None
        assert payload["analysis-auto-refresh"]["value"] == []
        assert payload["analysis-include"]["value"] == []
        assert payload["analysis-expand"]["value"] == []

    def test_hydrated_state_matches_the_separate_syncs(self, authed, callback_map):
        client, _store = authed
        doc = default_view_state() | {
            "active": "series",
            "series": default_view_state()["series"] | {"keys": ["k1"]},
            "scope": default_scope_state() | {"include_archived": True, "expand": True},
        }
        response, payload = self._sync(
            client, callback_map, doc, key_options=[{"value": "k1"}]
        )
        assert response.status_code == 200
        assert payload["analysis-tabs"]["value"] == "series"
        assert payload["analysis-key"]["value"] == ["k1"]
        assert payload["analysis-include"]["value"] == ["archived"]
        assert payload["analysis-expand"]["value"] == ["expand"]

    def test_unloaded_key_options_hold_the_write(self, authed, callback_map):
        client, _store = authed
        doc = default_view_state()
        doc["series"]["keys"] = ["k1"]
        response, payload = self._sync(client, callback_map, doc)
        assert response.status_code == 200
        assert "analysis-key" not in payload

    def test_one_writer_for_all_control_values(self, callback_map):
        writers = [
            key
            for key in callback_map
            if _outputs_of(key) & {"analysis-include.value", "analysis-expand.value"}
        ]
        assert len(writers) == 1
        assert _outputs_of(writers[0]) == _MERGED_SYNC_OUTPUTS
        inputs = {spec["id"] for spec in callback_map[writers[0]]["inputs"]}
        assert "view-store" in inputs
