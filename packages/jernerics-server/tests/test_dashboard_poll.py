import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionEndEvent,
    ExecutionStartEvent,
    IngestRequest,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
)
from jernerics_server.dashboard import callbacks, workspace
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
TRIAL_A = uuid.UUID("cc100000-0000-4000-8000-000000000000")
EXEC_A = uuid.UUID("dd100000-0000-4000-8000-000000000000")
EXEC_B = uuid.UUID("dd200000-0000-4000-8000-000000000000")

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
        ),
        _event(
            ExecutionStartEvent,
            985,
            execution_id=EXEC_A,
            trial_id=TRIAL_A,
            hostname="node00",
            started_at=_BASE - timedelta(seconds=985),
        ),
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


def _start_execution(store: Store, execution_id: uuid.UUID, seconds_ago: int) -> None:
    result = IngestService(store).apply(
        IngestRequest(
            protocol_version=PROTOCOL_VERSION,
            events=[
                _event(
                    ExecutionStartEvent,
                    seconds_ago,
                    execution_id=execution_id,
                    trial_id=TRIAL_A,
                    hostname="node01",
                    started_at=_BASE - timedelta(seconds=seconds_ago),
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


def _dispatch(
    client, callback_map, wanted: set[str], inputs, state=(), changed=None, key=None
):
    key = key or next(k for k in callback_map if _outputs_of(k) == wanted)
    specs = [
        part.split("@")[0]
        for part in key.removeprefix("..").removesuffix("..").split("...")
        if part
    ]
    outputs = []
    for spec in specs:
        prop = spec.rsplit(".", 1)[1]
        raw = spec.rsplit(".", 1)[0]
        outputs.append({"id": raw, "property": prop})
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


_ROUTER_OUTPUTS = {
    "page-container.children",
    "poll.disabled",
    "view-store.data",
    "route-store.data",
    "overview-digest-store.data",
}

_VIEW_INPUT = {"id": "view-store", "property": "data", "value": None}


class TestOverviewPollCascade:
    """The overview page re-renders on a poll tick only when a stored
    fact in the visible scope changed; relative time never counts."""

    def _fire(self, client, cmap, *, tick, digest, search=None, pathname=WORKSPACE):
        inputs = [
            {"id": "url", "property": "pathname", "value": pathname},
            {"id": "poll", "property": "n_intervals", "value": 1 if tick else 0},
        ]
        return _dispatch(
            client,
            cmap,
            _ROUTER_OUTPUTS,
            inputs=inputs,
            state=[
                _VIEW_INPUT,
                {
                    "id": "route-store",
                    "property": "data",
                    "value": pathname if tick else None,
                },
                {"id": "overview-digest-store", "property": "data", "value": digest},
                {"id": "url", "property": "search", "value": search},
            ],
            changed=["poll.n_intervals"] if tick else ["url.pathname"],
        )

    @staticmethod
    def _html(response) -> str:
        return response.text.replace("\\u002f", "/").replace("\\/", "/")

    def test_render_mounts_the_page_and_voids_the_digest(self, authed, callback_map):
        client, _store = authed
        response, page = self._fire(client, callback_map, tick=False, digest=None)
        assert response.status_code == 200
        assert page["overview-digest-store"]["data"] is None
        assert page["route-store"]["data"] == WORKSPACE
        # the seeded sweep is terminal: nothing polls
        assert page["poll"]["disabled"] is True
        assert "Overview" in self._html(response)

    def test_tick_establishes_digest_then_a_static_tick_ships_nothing(
        self, authed, callback_map
    ):
        client, _store = authed
        response, _page = self._fire(client, callback_map, tick=False, digest=None)
        assert response.status_code == 200
        # first tick: the stored digest is void (None), so the page re-ships
        response, page = self._fire(client, callback_map, tick=True, digest=None)
        assert response.status_code == 200
        digest_doc = page["overview-digest-store"]["data"]
        assert digest_doc and digest_doc["digest"]
        assert set(page) >= {"page-container", "poll"}
        # the next tick, with nothing changed server-side, ships nothing
        tick = self._fire(client, callback_map, tick=True, digest=digest_doc)
        assert tick[0].status_code == 204

    def test_changed_data_advances_digest_and_re_renders(self, authed, callback_map):
        client, store = authed
        self._fire(client, callback_map, tick=False, digest=None)
        _response, page = self._fire(client, callback_map, tick=True, digest=None)
        stale = page["overview-digest-store"]["data"]
        _ingest_sweep(store, SWEEP_B, "beta")
        response, _page = self._fire(client, callback_map, tick=True, digest=stale)
        assert response.status_code == 200
        assert "beta" in self._html(response)

    def test_wall_clock_advance_alone_keeps_the_digest(self, authed, callback_map):
        """jernerics-l4k root cause: relative-time churn must not move
        the digest — a tick two minutes later ships nothing."""
        client, _store = authed
        self._fire(client, callback_map, tick=False, digest=None)
        _response, page = self._fire(client, callback_map, tick=True, digest=None)
        stale = page["overview-digest-store"]["data"]
        later = time.time_ns() + 120_000_000_000
        with mock.patch("time.time_ns", return_value=later):
            tick = self._fire(client, callback_map, tick=True, digest=stale)
        assert tick[0].status_code == 204

    def test_unchanged_tick_builds_no_tree(self, authed, callback_map, monkeypatch):
        client, _store = authed
        builds = []
        real = workspace.overview_page

        def spy(*args, **kwargs):
            builds.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(workspace, "overview_page", spy)
        self._fire(client, callback_map, tick=False, digest=None)
        assert len(builds) == 1
        _response, page = self._fire(client, callback_map, tick=True, digest=None)
        digest_doc = page["overview-digest-store"]["data"]
        assert len(builds) == 2
        tick = self._fire(client, callback_map, tick=True, digest=digest_doc)
        assert tick[0].status_code == 204
        assert len(builds) == 2

    def test_work_in_flight_keeps_the_poll_alive(self, authed, callback_map):
        client, store = authed
        _start_execution(store, EXEC_B, 10)
        response, page = self._fire(client, callback_map, tick=False, digest=None)
        assert response.status_code == 200
        assert page["poll"]["disabled"] is False

    def test_search_parameters_drive_the_rendered_scope(self, authed, callback_map):
        client, store = authed
        store.archive_sweep(str(SWEEP_A))
        response, _page = self._fire(client, callback_map, tick=False, digest=None)
        text = self._html(response)
        assert "Active sweeps" in text
        assert "hides 1 archived/invalid" in text
        response, _page = self._fire(
            client, callback_map, tick=False, digest=None, search="?scope=all"
        )
        text = self._html(response)
        assert "All sweeps" in text
        assert "including 1 archived/invalid" in text


class TestGatePolling:
    """The interval gate re-evaluates on navigation, search, and view
    edits, writes the facts it decided from, and skips unchanged
    store-write dispatches."""

    _GATE_OUTPUTS = {"poll.disabled", "poll-gate-facts-store.data"}

    def _gate(self, client, cmap, *, search="", facts=None, changed=None):
        inputs = [
            {"id": "url", "property": "pathname", "value": WORKSPACE},
            {"id": "url", "property": "search", "value": search},
            _VIEW_INPUT,
        ]
        return _dispatch(
            client,
            cmap,
            self._GATE_OUTPUTS,
            inputs=inputs,
            state=[{"id": "poll-gate-facts-store", "property": "data", "value": facts}],
            changed=changed or ["url.search"],
        )

    def test_gate_disables_when_the_scope_is_terminal(self, authed, callback_map):
        client, _store = authed
        response, payload = self._gate(client, callback_map)
        assert response.status_code == 200
        assert payload["poll"]["disabled"] is True
        assert payload["poll-gate-facts-store"]["data"]["search"] == ""

    def test_gate_enables_while_work_is_in_flight(self, authed, callback_map):
        client, store = authed
        _start_execution(store, EXEC_B, 10)
        response, payload = self._gate(client, callback_map)
        assert response.status_code == 200
        assert payload["poll"]["disabled"] is False

    def test_unchanged_facts_on_a_store_write_skip_the_gate(self, authed, callback_map):
        client, _store = authed
        _response, payload = self._gate(client, callback_map)
        facts = payload["poll-gate-facts-store"]["data"]
        again = self._gate(
            client, callback_map, facts=facts, changed=["view-store.data"]
        )
        assert again[0].status_code == 204

    def test_a_search_change_re_evaluates(self, authed, callback_map):
        client, _store = authed
        _response, payload = self._gate(client, callback_map)
        facts = payload["poll-gate-facts-store"]["data"]
        response, _payload = self._gate(
            client, callback_map, search="?scope=all", facts=facts
        )
        assert response.status_code == 200


class TestOverviewFactsPure:
    """The overview digest reads stored facts only: no rendered tree and
    no wall-clock-derived string can reach it (jernerics-l4k)."""

    def test_facts_carry_no_relative_time_and_digest_is_stable(self, authed):
        _client, store = authed
        service = DashboardService(QueryService(store))
        facts = callbacks.overview_facts(service, PROJECT)
        assert "ago" not in json.dumps(facts, default=str)
        assert callbacks._content_digest(facts) == callbacks._content_digest(
            callbacks.overview_facts(service, PROJECT)
        )

    def test_scope_all_and_active_scopes_differ(self, authed):
        _client, store = authed
        store.archive_sweep(str(SWEEP_A))
