"""End-to-end shipper contract against the real in-process v3 server.

Proves the client's wire contract (batched ``IngestRequest`` with
``protocol_version=3``, cursor advance after ack, duplicate-safe overlap
between the live shipper and replay) holds against the actual
``jernerics_server`` app, not a mock.
"""

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from jernerics.tracking.batch_sync import replay_tracking
from jernerics.tracking.jsonl_io import (
    TrackingWriter,
    cursor_path,
    read_cursor,
    write_cursor,
)
from jernerics.tracking.stream_client import StreamClient
from jernerics_schema import (
    ExecutionEndEvent,
    ExecutionOutcome,
    ExecutionStartEvent,
    SweepSnapshotEvent,
    TrackingEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
)
from jernerics_server.http import create_app
from jernerics_server.store import Store

BASE_URL = "http://testserver"


class ServerTransport:
    """Adapts an in-process FastAPI TestClient to the shipper transport."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def post(self, url, *, content, headers, timeout):
        path = "/" + url.partition("://")[2].split("/", 1)[1]
        return self.client.post(path, content=content, headers=headers)


def _make_events() -> tuple[list[TrackingEvent], dict]:
    sweep_id = uuid4()
    trial_id = uuid4()
    execution_id = uuid4()
    now = datetime.now(timezone.utc)

    def stamped(model, **kwargs):
        return model(event_id=uuid4(), recorded_at=now, **kwargs)

    events = [
        stamped(
            SweepSnapshotEvent,
            sweep_id=sweep_id,
            project="proj",
            name="study",
            state="running",
        ),
        stamped(
            TrialSnapshotEvent,
            trial_id=trial_id,
            sweep_id=sweep_id,
            number=0,
            state=TrialState.RUNNING,
            retry_root_trial_id=trial_id,
        ),
        stamped(
            ExecutionStartEvent,
            execution_id=execution_id,
            trial_id=trial_id,
            hostname="test-host",
            started_at=now,
        ),
        stamped(ValueEvent, trial_id=trial_id, key="loss", step=0, value=0.5),
        stamped(ValueEvent, trial_id=trial_id, key="loss", step=1, value=0.4),
        stamped(
            ValueEvent,
            trial_id=trial_id,
            key="results",
            step=0,
            observation={"loss": 0.4},
        ),
        stamped(
            ExecutionEndEvent,
            execution_id=execution_id,
            ended_at=now,
            outcome=ExecutionOutcome.SUCCESS,
        ),
    ]
    return events, {"trial_id": trial_id, "execution_id": execution_id}


def _write_log(path: Path, events: Sequence[TrackingEvent]) -> None:
    with TrackingWriter(path) as writer:
        for event in events:
            writer.write_event(event)


class TestLiveShipperAgainstRealServer:
    def test_ships_batch_and_applies_events(self, tmp_path: Path) -> None:
        store = Store(tmp_path / "server.db")
        client = TestClient(create_app(store))
        events, ids = _make_events()
        path = tmp_path / "study" / "events" / "0.jsonl"
        _write_log(path, events)

        shipper = StreamClient(
            BASE_URL,
            path,
            poll_interval=0.05,
            flush_timeout=5.0,
            batch_window=0.2,
            transport=ServerTransport(client),
        )
        shipper.start()
        shipper.join()

        assert read_cursor(path) == path.stat().st_size
        _, rows = store.query(
            "SELECT COUNT(*) FROM tracked_values v "
            "JOIN executions e ON v.execution_id = e.execution_id "
            "WHERE e.trial_id = ?",
            [str(ids["trial_id"])],
        )
        assert rows[0][0] == 3
        _, exec_rows = store.query(
            "SELECT outcome FROM executions WHERE execution_id = ?",
            [str(ids["execution_id"])],
        )
        assert exec_rows[0][0] == "success"


class TestOverlapIsDuplicateSafe:
    def test_replay_after_live_ship_reports_all_duplicates(self, tmp_path: Path):
        store = Store(tmp_path / "server.db")
        client = TestClient(create_app(store))
        transport = ServerTransport(client)
        events, _ = _make_events()
        path = tmp_path / "study" / "events" / "0.jsonl"
        _write_log(path, events)

        # live shipper delivers everything
        shipper = StreamClient(
            BASE_URL,
            path,
            poll_interval=0.05,
            flush_timeout=5.0,
            batch_window=0.2,
            transport=transport,
        )
        shipper.start()
        shipper.join()
        assert read_cursor(path) == path.stat().st_size

        # a fresh replay re-sends the whole file from offset 0
        write_cursor(path, 0)
        result = replay_tracking(
            tracking_dir=tmp_path,
            base_url=BASE_URL,
            study="study",
            transport=transport,
        )
        assert result.errors == []
        assert result.events_sent == len(events)

        # one more overlapping batch must be all duplicates
        body = json.dumps(
            {
                "protocol_version": 3,
                "events": [json.loads(event.model_dump_json()) for event in events],
            }
        )
        response = client.post(
            "/ingest", content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 200
        ack = response.json()
        assert ack["accepted"] == 0
        assert ack["duplicates"] == len(events)


class TestReplayBatchesAgainstRealServer:
    def test_replay_delivers_and_reports_cleanly(self, tmp_path: Path):
        store = Store(tmp_path / "server.db")
        client = TestClient(create_app(store))
        events, ids = _make_events()
        path = tmp_path / "study" / "events" / "0.jsonl"
        _write_log(path, events)

        result = replay_tracking(
            tracking_dir=tmp_path,
            base_url=BASE_URL,
            study="study",
            transport=ServerTransport(client),
        )

        assert result.errors == []
        assert result.events_sent == len(events)
        assert not path.exists()
        assert not cursor_path(path).exists()

        _, rows = store.query(
            "SELECT COUNT(*) FROM executions WHERE trial_id = ?",
            [str(ids["trial_id"])],
        )
        assert rows[0][0] == 1

    def test_replay_is_idempotent_across_runs(self, tmp_path: Path):
        client = TestClient(create_app(Store(tmp_path / "server.db")))
        events, _ = _make_events()
        path = tmp_path / "study" / "events" / "0.jsonl"
        _write_log(path, events)

        transport = ServerTransport(client)
        first = replay_tracking(
            tracking_dir=tmp_path,
            base_url=BASE_URL,
            study="study",
            transport=transport,
        )
        # recreate the file (replay deletes on success) and ship it again
        _write_log(path, events)
        second = replay_tracking(
            tracking_dir=tmp_path,
            base_url=BASE_URL,
            study="study",
            transport=transport,
        )

        assert first.events_sent == len(events)
        assert second.events_sent == len(events)
        assert second.errors == []
