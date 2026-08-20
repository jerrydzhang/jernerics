import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from jernerics.tracking.batch_sync import (
    FileResult,
    _replay_file,
    discover_jsonl_files,
    replay_tracking,
)
from jernerics.tracking.jsonl_io import (
    TrackingWriter,
    cursor_path,
    read_cursor,
    scan_events,
)
from jernerics_schema import TrackingEvent, ValueEvent

BASE_URL = "http://localhost:8000"


@dataclass
class _FakeResponse:
    status_code: int
    content: bytes = b'{"accepted": 0}'


class FakeTransport:
    def __init__(self, responses=None, always=None):
        self.requests: list[tuple[str, str, dict]] = []
        self.responses = list(responses or [])
        self.always = always

    def post(self, url, *, content, headers, timeout):
        self.requests.append((url, content, dict(headers or {})))
        if self.responses:
            response = self.responses.pop(0)
        else:
            response = self.always if self.always is not None else 200
        if isinstance(response, Exception):
            raise response
        if isinstance(response, _FakeResponse):
            return response
        return _FakeResponse(response)

    @property
    def bodies(self) -> list[str]:
        return [content for _, content, _ in self.requests]


def _value_event(key: str = "loss", value: float = 0.5) -> ValueEvent:
    return ValueEvent(
        event_id=uuid4(),
        recorded_at=datetime.now(timezone.utc),
        trial_id=uuid4(),
        key=key,
        step=0,
        value=value,
    )


def _write_events(path: Path, events: Sequence[TrackingEvent]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TrackingWriter(path) as writer:
        for event in events:
            writer.write_event(event)
    return path


def _ids(body: str) -> list[str]:
    return [event["event_id"] for event in json.loads(body)["events"]]


class TestReplayFile:
    def test_ships_all_events_in_one_batch(self, tmp_path: Path) -> None:
        events = [_value_event(f"m{i}") for i in range(5)]
        path = _write_events(tmp_path / "0.jsonl", events)
        transport = FakeTransport()

        result = _replay_file(path, BASE_URL, transport=transport)

        assert result.error is None
        assert result.events_sent == 5
        assert result.events_total == 5
        assert len(transport.requests) == 1
        assert len(json.loads(transport.bodies[0])["events"]) == 5
        assert read_cursor(path) == path.stat().st_size

    def test_splits_into_max_size_batches(self, tmp_path: Path) -> None:
        events = [_value_event(f"m{i}") for i in range(250)]
        path = _write_events(tmp_path / "0.jsonl", events)
        transport = FakeTransport()

        result = _replay_file(path, BASE_URL, transport=transport)

        sizes = [len(json.loads(body)["events"]) for body in transport.bodies]
        assert sizes == [100, 100, 50]
        assert result.events_sent == 250
        assert read_cursor(path) == path.stat().st_size

    def test_collects_conflicts_from_acknowledged_response(self, tmp_path) -> None:
        from jernerics_schema import ConflictRecord

        trial = uuid4()
        conflict = ConflictRecord(
            trial_id=trial,
            kind="optimizer_terminal_state",
            detail='{"existing": "completed", "incoming": "failed"}',
        )
        body = json.dumps(
            {
                "accepted": 1,
                "duplicates": 0,
                "conflicts": [
                    {
                        "trial_id": str(trial),
                        "kind": conflict.kind,
                        "detail": conflict.detail,
                    }
                ],
            }
        ).encode()
        path = _write_events(tmp_path / "0.jsonl", [_value_event()])
        transport = FakeTransport(responses=[_FakeResponse(200, content=body)])

        result = _replay_file(path, BASE_URL, transport=transport)

        assert result.error is None
        assert result.conflicts == [conflict]

        aggregated = replay_tracking(
            tracking_dir=tmp_path, base_url=BASE_URL, transport=FakeTransport()
        )
        assert aggregated.conflicts == []

    def test_starts_at_cursor_and_ships_only_remainder(self, tmp_path: Path) -> None:
        events = [_value_event(f"m{i}") for i in range(10)]
        path = _write_events(tmp_path / "0.jsonl", events)
        scanned, _ = scan_events(path, 0, max_events=4)
        from jernerics.tracking.jsonl_io import write_cursor

        write_cursor(path, scanned[3][1])
        transport = FakeTransport()

        result = _replay_file(path, BASE_URL, transport=transport)

        assert _ids(transport.bodies[0]) == [
            str(event.event_id) for event in events[4:]
        ]
        assert result.events_sent == 6
        assert result.events_total == 10

    def test_failed_batch_retries_same_body_then_advances_once(
        self, tmp_path: Path
    ) -> None:
        path = _write_events(tmp_path / "0.jsonl", [_value_event()])
        transport = FakeTransport([500, 502])

        result = _replay_file(path, BASE_URL, transport=transport)

        assert result.error is None
        assert transport.bodies[0] == transport.bodies[1] == transport.bodies[2]
        assert read_cursor(path) == path.stat().st_size

    def test_retry_budget_exhaustion_reports_error_and_keeps_cursor(
        self, tmp_path: Path
    ) -> None:
        events = [_value_event(f"m{i}") for i in range(150)]
        path = _write_events(tmp_path / "0.jsonl", events)
        # batch 1 succeeds; batch 2 exhausts its retry budget on HTTP 500
        transport = FakeTransport([200], always=500)

        result = _replay_file(path, BASE_URL, transport=transport, max_retries=2)

        assert result.error is not None
        assert result.events_sent == 100
        assert result.events_total == 150
        scanned, _ = scan_events(path, 0, max_events=100)
        assert read_cursor(path) == scanned[99][1]
        # the same unacked batch was retried byte-identically
        for retried in transport.bodies[1:]:
            assert retried == transport.bodies[1]

    def test_offline_server_reports_error_without_data_loss(
        self, tmp_path: Path
    ) -> None:
        path = _write_events(tmp_path / "0.jsonl", [_value_event()])
        transport = FakeTransport(always=httpx.ConnectError("refused"))

        result = _replay_file(path, BASE_URL, transport=transport, max_retries=2)

        assert result.error is not None
        assert "refused" in result.error
        assert result.events_sent == 0
        assert path.exists()
        assert read_cursor(path) == 0

    def test_auth_header_sent_when_api_key_given(self, tmp_path: Path) -> None:
        path = _write_events(tmp_path / "0.jsonl", [_value_event()])
        transport = FakeTransport()

        _replay_file(path, BASE_URL, api_key="secret", transport=transport)

        headers = transport.requests[0][2]
        assert headers["authorization"] == "Bearer secret"
        assert headers["content-type"] == "application/json"

    def test_request_carries_protocol_version_3(self, tmp_path: Path) -> None:
        path = _write_events(tmp_path / "0.jsonl", [_value_event()])
        transport = FakeTransport()

        _replay_file(path, BASE_URL, transport=transport)

        assert json.loads(transport.bodies[0])["protocol_version"] == 3


class TestDiscoverJsonlFiles:
    def test_finds_all_studies(self, tmp_path: Path) -> None:
        _write_events(tmp_path / "alpha" / "events" / "0.jsonl", [_value_event()])
        _write_events(tmp_path / "beta" / "events" / "0.jsonl", [_value_event()])
        _write_events(tmp_path / "beta" / "events" / "1.jsonl", [_value_event()])

        found = discover_jsonl_files(tmp_path)

        assert [p.name for p in found] == ["0.jsonl", "0.jsonl", "1.jsonl"]

    def test_ignores_cursor_sidecars(self, tmp_path: Path) -> None:
        path = _write_events(
            tmp_path / "alpha" / "events" / "0.jsonl", [_value_event()]
        )
        cursor_path(path).write_text("0")

        assert discover_jsonl_files(tmp_path) == [path]

    def test_scopes_to_study(self, tmp_path: Path) -> None:
        alpha = _write_events(
            tmp_path / "alpha" / "events" / "0.jsonl", [_value_event()]
        )
        _write_events(tmp_path / "beta" / "events" / "0.jsonl", [_value_event()])

        assert discover_jsonl_files(tmp_path, study="alpha") == [alpha]

    def test_empty_dir_returns_empty_list(self, tmp_path: Path) -> None:
        assert discover_jsonl_files(tmp_path) == []

    def test_finds_submission_logs(self, tmp_path: Path) -> None:
        submission = _write_events(
            tmp_path / "alpha" / "submission" / "abc.jsonl", [_value_event()]
        )

        assert discover_jsonl_files(tmp_path) == [submission]

    def test_scopes_submission_logs_to_study(self, tmp_path: Path) -> None:
        alpha = _write_events(
            tmp_path / "alpha" / "submission" / "abc.jsonl", [_value_event()]
        )
        _write_events(tmp_path / "beta" / "submission" / "def.jsonl", [_value_event()])

        assert discover_jsonl_files(tmp_path, study="alpha") == [alpha]

    def test_merges_events_and_submission_logs_sorted(self, tmp_path: Path) -> None:
        events = _write_events(
            tmp_path / "alpha" / "events" / "0.jsonl", [_value_event()]
        )
        submission = _write_events(
            tmp_path / "alpha" / "submission" / "abc.jsonl", [_value_event()]
        )

        found = discover_jsonl_files(tmp_path)

        assert found == sorted([events, submission])
        assert set(found) == {events, submission}


class TestReplayTracking:
    def test_ships_every_file_and_deletes_files_and_cursors(
        self, tmp_path, capfd
    ) -> None:
        events_dir = tmp_path / "study" / "events"
        path = _write_events(events_dir / "0.jsonl", [_value_event()])
        cursor_path(path).write_text("0")

        result = replay_tracking(
            tracking_dir=tmp_path,
            base_url=BASE_URL,
            study="study",
            transport=FakeTransport(),
        )

        assert result.files_processed == 1
        assert result.events_sent == 1
        assert result.errors == []
        assert not path.exists()
        assert not cursor_path(path).exists()
        assert "Done." in capfd.readouterr().err

    def test_error_keeps_files_and_reports_per_file(self, tmp_path, capfd) -> None:
        path = _write_events(
            tmp_path / "study" / "events" / "0.jsonl", [_value_event()]
        )

        result = replay_tracking(
            tracking_dir=tmp_path,
            base_url=BASE_URL,
            max_retries=2,
            transport=FakeTransport(always=httpx.ConnectError("refused")),
        )

        assert result.files_processed == 1
        assert result.events_failed == 1
        assert len(result.errors) == 1
        assert str(path) in result.errors[0]
        assert path.exists()
        assert read_cursor(path) == 0
        assert "FAIL" in capfd.readouterr().err

    def test_no_files_reports_and_returns_empty(self, tmp_path, capfd) -> None:
        result = replay_tracking(
            tracking_dir=tmp_path, base_url=BASE_URL, transport=FakeTransport()
        )

        assert result == type(result)()
        assert "No .jsonl files found." in capfd.readouterr().err

    def test_already_shipped_file_reports_zero_sent_and_is_deleted(
        self, tmp_path
    ) -> None:
        path = _write_events(
            tmp_path / "study" / "events" / "0.jsonl", [_value_event()]
        )
        from jernerics.tracking.jsonl_io import write_cursor

        write_cursor(path, path.stat().st_size)

        result = replay_tracking(
            tracking_dir=tmp_path,
            base_url=BASE_URL,
            transport=FakeTransport(),
        )

        assert result.events_sent == 0
        assert result.errors == []
        assert not path.exists()


class TestFileResultShape:
    def test_defaults(self, tmp_path: Path) -> None:
        result = FileResult(path=tmp_path / "x.jsonl")

        assert result.events_sent == 0
        assert result.events_total == 0
        assert result.error is None

    def test_missing_file_is_a_clean_no_op(self, tmp_path) -> None:
        result = _replay_file(tmp_path / "missing.jsonl", BASE_URL)

        assert result.error is None
        assert result.events_sent == 0
        assert result.events_total == 0


def test_replay_uses_batches_of_at_most_100(tmp_path: Path) -> None:
    from jernerics_schema.ingest import MAX_EVENTS_PER_REQUEST

    assert MAX_EVENTS_PER_REQUEST == 100
