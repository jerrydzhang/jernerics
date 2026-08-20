import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from jernerics.tracking.jsonl_io import read_cursor, write_cursor
from jernerics.tracking.stream_client import StreamClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    TrackingEvent,
    ValueEvent,
)

BASE_URL = "http://localhost:8000"


@dataclass
class _FakeResponse:
    status_code: int


class FakeTransport:
    """Records requests; replies from a scripted list of statuses/exceptions."""

    def __init__(self, responses=None, always=None):
        self.requests: list[tuple[str, str, dict, float]] = []
        self.responses = list(responses or [])
        self.always = always

    def post(self, url, *, content, headers, timeout):
        self.requests.append((url, content, dict(headers or {}), timeout))
        if self.responses:
            response = self.responses.pop(0)
        else:
            response = self.always if self.always is not None else 200
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)

    @property
    def bodies(self) -> list[str]:
        return [content for _, content, _, _ in self.requests]


def _value_event(key: str = "loss", value: float = 0.5) -> ValueEvent:
    return ValueEvent(
        event_id=uuid4(),
        recorded_at=datetime.now(timezone.utc),
        trial_id=uuid4(),
        key=key,
        step=0,
        value=value,
    )


def _write_events(path: Path, events: Sequence[TrackingEvent]) -> None:
    from jernerics.tracking.jsonl_io import TrackingWriter

    with TrackingWriter(path) as writer:
        for event in events:
            writer.write_event(event)


def _make_client(
    path: Path,
    transport: FakeTransport,
    **kwargs: Any,
) -> StreamClient:
    options: dict[str, Any] = {
        "poll_interval": 0.05,
        "flush_timeout": 5.0,
        "send_deadline": 2.0,
        "max_retry_time": 1.0,
        "batch_window": 0.3,
        "transport": transport,
    }
    options.update(kwargs)
    return StreamClient(BASE_URL, path, **options)


def _ids(body: str) -> list[str]:
    return [event["event_id"] for event in json.loads(body)["events"]]


def _wait_for(predicate, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {what}")


class TestBatching:
    def test_partial_batch_flushes_after_window(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event(f"m{i}") for i in range(3)])
        transport = FakeTransport()
        client = _make_client(path, transport)

        started = time.monotonic()
        client.start()
        _wait_for(lambda: transport.requests, what="first flush")
        elapsed = time.monotonic() - started
        client.join()

        assert len(transport.requests) == 1
        assert len(json.loads(transport.bodies[0])["events"]) == 3
        # flush was triggered by the 300ms window, not by reaching batch size
        assert elapsed >= 0.2

    def test_full_batch_flushes_immediately(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event(f"m{i}") for i in range(100)])
        transport = FakeTransport()
        client = _make_client(path, transport, batch_window=10.0)

        started = time.monotonic()
        client.start()
        _wait_for(lambda: transport.requests, what="full batch flush")
        elapsed = time.monotonic() - started
        client.join()

        assert len(transport.requests) == 1
        assert len(json.loads(transport.bodies[0])["events"]) == 100
        assert elapsed < 5.0

    def test_more_than_batch_size_splits_into_batches(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event(f"m{i}") for i in range(250)])
        transport = FakeTransport()
        client = _make_client(path, transport)

        client.start()
        _wait_for(lambda: len(transport.requests) == 3, what="three batches")
        client.join()

        sizes = [len(json.loads(body)["events"]) for body in transport.bodies]
        assert sizes == [100, 100, 50]
        assert read_cursor(path) == path.stat().st_size


class TestRequestShape:
    def test_protocol_version_and_headers(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event()])
        transport = FakeTransport()
        client = _make_client(path, transport, api_key="secret")
        client.start()
        _wait_for(lambda: transport.requests, what="request")
        client.join()

        url, body, headers, timeout = transport.requests[0]
        assert url == f"{BASE_URL}/ingest"
        assert json.loads(body)["protocol_version"] == PROTOCOL_VERSION == 3
        assert headers["content-type"] == "application/json"
        assert headers["authorization"] == "Bearer secret"
        assert timeout == client.send_deadline

    def test_cursor_advances_to_acknowledged_offset(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event(f"m{i}") for i in range(5)])
        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()
        _wait_for(lambda: transport.requests, what="ack")
        client.join()

        assert read_cursor(path) == path.stat().st_size


class TestRetry:
    def test_failed_batch_retries_same_body_and_advances_once(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event(f"m{i}") for i in range(4)])
        transport = FakeTransport([500, 200])
        client = _make_client(path, transport)
        client.start()
        _wait_for(lambda: len(transport.requests) == 2, what="retry")
        client.join()

        assert transport.bodies[0] == transport.bodies[1]
        assert read_cursor(path) == path.stat().st_size
        assert len(transport.requests) == 2

    def test_connection_errors_retry_with_same_event_ids(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event(f"m{i}") for i in range(4)])
        transport = FakeTransport(
            [httpx.ConnectError("down"), httpx.ConnectError("down"), 200]
        )
        client = _make_client(path, transport)
        client.start()
        _wait_for(lambda: len(transport.requests) == 3, what="retries")
        client.join()

        first_ids = _ids(transport.bodies[0])
        assert _ids(transport.bodies[1]) == first_ids
        assert _ids(transport.bodies[2]) == first_ids
        assert read_cursor(path) == path.stat().st_size

    def test_gives_up_after_max_retry_time_leaving_cursor(
        self, tmp_path, capfd
    ) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event()])
        transport = FakeTransport(always=httpx.ConnectError("down"))
        client = _make_client(path, transport, max_retry_time=0.2)

        client.start()
        client._thread.join(timeout=5.0)

        assert not client._thread.is_alive()
        assert read_cursor(path) == 0
        assert "max retry time" in capfd.readouterr().err


class TestCrashRestart:
    def test_new_instance_resumes_at_cursor_without_reshipping(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "events.jsonl"
        events = [_value_event(f"m{i}") for i in range(150)]
        _write_events(path, events)

        # first instance: batch 1 (100 events) acks; the server then dies
        first_transport = FakeTransport([200], always=httpx.ConnectError("gone"))
        first = _make_client(path, first_transport, max_retry_time=0.3)
        first.start()
        _wait_for(lambda: len(first_transport.requests) >= 2, what="failed batch")
        first.join()  # drain fails; thread exits via retry budget
        first._thread.join(timeout=10.0)
        assert not first._thread.is_alive()

        acked_ids = _ids(first_transport.bodies[0])
        assert len(acked_ids) == 100
        assert read_cursor(path) != 0
        for retried in first_transport.bodies[1:]:
            assert _ids(retried) == [str(event.event_id) for event in events[100:]]

        # second instance (fresh process stand-in): only the remainder ships,
        # with byte-identical event ids from the JSONL
        second_transport = FakeTransport()
        second = _make_client(path, second_transport)
        second.start()
        _wait_for(lambda: second_transport.requests, what="resume flush")
        second.join()

        assert len(second_transport.requests) == 1
        resumed_ids = _ids(second_transport.bodies[0])
        expected_ids = [str(event.event_id) for event in events[100:]]
        assert resumed_ids == expected_ids
        assert not set(resumed_ids) & set(acked_ids)
        assert read_cursor(path) == path.stat().st_size


class TestShutdown:
    def test_join_drains_pending_events(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        transport = FakeTransport()
        client = _make_client(path, transport, batch_window=60.0)
        client.start()

        _write_events(path, [_value_event(f"m{i}") for i in range(3)])
        client.join()

        assert len(transport.requests) == 1
        assert len(json.loads(transport.bodies[0])["events"]) == 3
        assert read_cursor(path) == path.stat().st_size

    def test_join_with_offline_server_exits_and_keeps_cursor(
        self, tmp_path, capfd
    ) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event()])
        transport = FakeTransport(always=httpx.ConnectError("down"))
        client = _make_client(
            path,
            transport,
            flush_timeout=0.4,
            max_retry_time=60.0,
        )

        client.start()
        _wait_for(lambda: transport.requests, what="first attempt")
        client.join()  # returns within flush timeout
        client._thread.join(timeout=5.0)

        assert not client._thread.is_alive()
        assert read_cursor(path) == 0
        assert (tmp_path / "events.jsonl").exists()

    def test_join_without_start_is_noop(self, tmp_path: Path) -> None:
        client = _make_client(tmp_path / "events.jsonl", FakeTransport())
        client.join()


class TestLiveTail:
    def test_deferred_file_creation(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()

        assert not transport.requests
        _write_events(path, [_value_event()])
        _wait_for(lambda: transport.requests, what="flush after create")
        client.join()

        assert len(transport.requests) == 1

    def test_partial_line_waited_until_complete(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()

        first = _value_event()
        second = _value_event("acc")
        second_line = second.model_dump_json() + "\n"
        with open(path, "a") as f:
            f.write(first.model_dump_json() + "\n")
            f.write(second_line[:20])

        _wait_for(lambda: transport.requests, what="first event flush")
        client.join()
        assert len(transport.requests) == 1
        assert _ids(transport.bodies[0]) == [str(first.event_id)]

        with open(path, "a") as f:
            f.write(second_line[20:])
        transport2 = FakeTransport()
        client2 = _make_client(path, transport2)
        client2.start()
        _wait_for(lambda: transport2.requests, what="completed line flush")
        client2.join()
        assert _ids(transport2.bodies[0]) == [str(second.event_id)]


class TestCursorInterplay:
    def test_resumes_from_preexisting_cursor_not_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        events = [_value_event(f"m{i}") for i in range(6)]
        _write_events(path, events)
        from jernerics.tracking.jsonl_io import scan_events

        scanned, _ = scan_events(path, 0, max_events=4)
        write_cursor(path, scanned[3][1])

        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()
        _wait_for(lambda: transport.requests, what="flush")
        client.join()

        assert _ids(transport.bodies[0]) == [
            str(event.event_id) for event in events[4:]
        ]

    def test_empty_file_ships_nothing(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        path.touch()
        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()
        time.sleep(0.2)
        client.join()

        assert not transport.requests


class TestBadLines:
    def test_corrupt_complete_line_stops_shipper_without_advancing_cursor(
        self, tmp_path, capfd
    ) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event()])
        with open(path, "a") as f:
            f.write('{"tag": "value"}\n')  # complete but invalid

        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()
        client._thread.join(timeout=5.0)

        assert not transport.requests
        assert read_cursor(path) == 0
        assert "shipper stopped" in capfd.readouterr().err


class TestAuthOptional:
    def test_no_api_key_omits_authorization_header(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        _write_events(path, [_value_event()])
        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()
        _wait_for(lambda: transport.requests, what="request")
        client.join()

        assert "authorization" not in transport.requests[0][2]


class TestTwoShippersOneFile:
    def test_second_shipper_reships_only_unacked(self, tmp_path: Path) -> None:
        path = tmp_path / "events.jsonl"
        events = [_value_event(f"m{i}") for i in range(10)]
        _write_events(path, events)
        from jernerics.tracking.jsonl_io import scan_events

        scanned, _ = scan_events(path, 0, max_events=5)
        write_cursor(path, scanned[4][1])

        transport = FakeTransport()
        client = _make_client(path, transport)
        client.start()
        _wait_for(lambda: transport.requests, what="flush")
        client.join()

        assert _ids(transport.bodies[0]) == [
            str(event.event_id) for event in events[5:]
        ]
