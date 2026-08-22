import fcntl
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from jernerics.tracking.jsonl_io import (
    TrackingReader,
    TrackingWriter,
    cursor_lock_path,
    cursor_path,
    read_cursor,
    scan_events,
    write_cursor,
)
from jernerics_schema import (
    ManualParamEvent,
    TrackingEvent,
    ValueEvent,
)
from pydantic import TypeAdapter, ValidationError

_ADAPTER = TypeAdapter(TrackingEvent)


def _param_event(key: str = "lr", value: float = 0.1) -> ManualParamEvent:
    return ManualParamEvent(
        event_id=uuid4(),
        recorded_at=datetime.now(timezone.utc),
        trial_id=uuid4(),
        key=key,
        value=value,
    )


def _value_event(key: str = "loss", value: float = 0.5) -> ValueEvent:
    return ValueEvent(
        event_id=uuid4(),
        recorded_at=datetime.now(timezone.utc),
        trial_id=uuid4(),
        key=key,
        step=0,
        value=value,
    )


def read_all(path: Path) -> list[TrackingEvent]:
    with TrackingReader(path) as reader:
        return list(reader)


class TestRoundTrip:
    def test_single_event(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        event = _param_event()

        with TrackingWriter(p) as writer:
            writer.write_event(event)

        assert read_all(p) == [event]

    def test_one_line_per_event(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        events = [_param_event(), _value_event(), _param_event()]

        with TrackingWriter(p) as writer:
            for event in events:
                writer.write_event(event)

        lines = p.read_text().splitlines()
        assert len(lines) == 3
        assert read_all(p) == events

    def test_every_event_parses_through_tracking_event_adapter(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "events.jsonl"
        events = [_param_event("a", 1), _value_event("b", 2.0)]

        with TrackingWriter(p) as writer:
            for event in events:
                writer.write_event(event)

        for line, source in zip(p.read_text().splitlines(), events, strict=True):
            assert _ADAPTER.validate_json(line) == source

    def test_order_preserved_over_many_writes(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        events = [_value_event("loss", float(i)) for i in range(50)]

        with TrackingWriter(p) as writer:
            for event in events:
                writer.write_event(event)

        events = read_all(p)
        assert all(isinstance(e, ValueEvent) for e in events)
        assert [e.value for e in events if isinstance(e, ValueEvent)] == [
            float(i) for i in range(50)
        ]

    def test_reopen_preserves_prior_data(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        first, second = _value_event("a"), _value_event("b")

        with TrackingWriter(p) as writer:
            writer.write_event(first)
        with TrackingWriter(p) as writer:
            writer.write_event(second)

        assert read_all(p) == [first, second]


class TestTryReadEvent:
    def test_empty_file_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.jsonl"
        p.touch()

        with TrackingReader(p) as reader:
            assert reader.try_read_event() is None

    def test_blank_line_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "blank.jsonl"
        p.write_text("\n")

        with TrackingReader(p) as reader:
            assert reader.try_read_event() is None

    def test_partial_line_returns_none_and_does_not_advance(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "partial.jsonl"
        event = _param_event()
        p.write_text(event.model_dump_json() + "\n")
        with open(p, "a") as f:
            f.write('{"event_id": "18d16093-4a')  # crash mid-write

        with TrackingReader(p) as reader:
            assert reader.try_read_event() == event
            pos_before = reader.file.tell()
            assert reader.try_read_event() is None
            assert reader.file.tell() == pos_before


class TestCursor:
    def test_missing_cursor_reads_zero(self, tmp_path: Path) -> None:
        assert read_cursor(tmp_path / "events.jsonl") == 0

    def test_write_then_read(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        write_cursor(p, 1234)
        assert read_cursor(p) == 1234

    def test_cursor_path_is_sidecar(self, tmp_path: Path) -> None:
        expected = tmp_path / "events.jsonl.cursor"
        assert cursor_path(tmp_path / "events.jsonl") == expected

    def test_write_cursor_leaves_no_temp_file(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        write_cursor(p, 10)
        write_cursor(p, 20)
        assert sorted(f.name for f in tmp_path.iterdir()) == [
            "events.jsonl.cursor",
            "events.jsonl.cursor.lock",
        ]

    def test_corrupt_cursor_reads_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        cursor_path(p).write_text("not-a-number")
        assert read_cursor(p) == 0

    def test_stale_older_offset_does_not_regress(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_bytes(b"x" * 600)
        write_cursor(p, 500)

        write_cursor(p, 200)

        assert read_cursor(p) == 500

    def test_smaller_offset_follows_recreated_file(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_bytes(b"x" * 600)
        write_cursor(p, 500)

        p.write_bytes(b"x" * 100)  # recreated smaller than the cursor
        write_cursor(p, 100)

        assert read_cursor(p) == 100

    def test_cursor_lock_path_is_sidecar_of_cursor(self, tmp_path: Path) -> None:
        expected = tmp_path / "events.jsonl.cursor.lock"
        assert cursor_lock_path(tmp_path / "events.jsonl") == expected


class TestScanEvents:
    def test_scans_from_offset_with_line_end_positions(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        events = [_value_event("a"), _value_event("b"), _value_event("c")]
        with TrackingWriter(p) as writer:
            for event in events:
                writer.write_event(event)

        scanned, offset = scan_events(p, 0)
        assert [event for event, _ in scanned] == events
        assert offset == p.stat().st_size
        assert scanned[0][1] < scanned[1][1] < scanned[2][1] == offset

        tail, _ = scan_events(p, scanned[0][1])
        assert [event for event, _ in tail] == events[1:]

    def test_resumes_at_cursor_offset(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        events = [_value_event(str(i)) for i in range(3)]
        with TrackingWriter(p) as writer:
            for event in events:
                writer.write_event(event)
        scanned, _ = scan_events(p, 0)
        write_cursor(p, scanned[0][1])

        scanned, _ = scan_events(p, read_cursor(p))
        assert [event for event, _ in scanned] == events[1:]

    def test_partial_trailing_line_not_consumed(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        first, second = _param_event(), _value_event("b")
        second_line = second.model_dump_json() + "\n"
        with open(p, "w") as f:
            f.write(first.model_dump_json() + "\n")
            f.write(second_line[: len(second_line) // 2])  # crash mid-write

        scanned, offset = scan_events(p, 0)
        assert [event for event, _ in scanned] == [first]
        assert offset == len(first.model_dump_json()) + 1

        # completing the line makes it visible
        with open(p, "a") as f:
            f.write(second_line[len(second_line) // 2 :])
        scanned, _ = scan_events(p, offset)
        assert [event for event, _ in scanned] == [second]

    def test_max_events_caps_the_scan(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        events = [_value_event(str(i)) for i in range(5)]
        with TrackingWriter(p) as writer:
            for event in events:
                writer.write_event(event)

        scanned, offset = scan_events(p, 0, max_events=2)
        assert [event for event, _ in scanned] == events[:2]
        scanned, _ = scan_events(p, offset)
        assert [event for event, _ in scanned] == events[2:]

    def test_missing_file_returns_nothing(self, tmp_path: Path) -> None:
        assert scan_events(tmp_path / "nope.jsonl", 0) == ([], 0)

    def test_offset_beyond_size_restarts_from_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        event = _value_event()
        with TrackingWriter(p) as writer:
            writer.write_event(event)

        scanned, _ = scan_events(p, p.stat().st_size + 500)
        assert [event for event, _ in scanned] == [event]

    def test_malformed_complete_line_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        p.write_text('{"tag": "nonsense"}\n')

        with pytest.raises(ValidationError):
            scan_events(p, 0)


class TestConcurrentWriters:
    def test_heartbeat_and_user_writes_interleave_as_whole_lines(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "events.jsonl"
        writer = TrackingWriter(p)
        barrier = threading.Barrier(2)

        def write_many(key: str) -> None:
            barrier.wait()
            for i in range(200):
                writer.write_event(_value_event(f"{key}{i}"))

        threads = [
            threading.Thread(target=write_many, args=("a",)),
            threading.Thread(target=write_many, args=("b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        writer.close()

        events = read_all(p)
        assert len(events) == 400
        assert len({event.event_id for event in events}) == 400


class TestConcurrentCursorCommits:
    def test_parallel_commits_keep_maximum_and_leave_no_temp_files(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "events.jsonl"
        p.write_bytes(b"x" * 200)
        offsets = list(range(1, 121))
        barrier = threading.Barrier(len(offsets))

        def commit(offset: int) -> None:
            barrier.wait()
            write_cursor(p, offset)

        threads = [
            threading.Thread(target=commit, args=(offset,)) for offset in offsets
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert read_cursor(p) == max(offsets)
        assert not list(tmp_path.glob("*.tmp"))


class TestWriterLifetimeLock:
    def test_exclusive_probe_blocks_while_writer_is_open(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        writer = TrackingWriter(p)

        fd = os.open(p, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

        writer.close()

        fd = os.open(p, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)


class TestWriterReopenRace:
    def test_writer_reopens_fresh_inode_when_replaced_between_open_and_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "events.jsonl"
        p.touch()
        stale_ino = p.stat().st_ino
        real_flock = fcntl.flock
        replace_once = [True]

        def flock_replacing_once(fd, operation):
            if replace_once[0]:
                replace_once[0] = False
                p.unlink()
                p.touch()
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", flock_replacing_once)
        writer = TrackingWriter(p)

        fresh_ino = os.stat(p).st_ino
        assert fresh_ino != stale_ino
        assert os.fstat(writer.file.fileno()).st_ino == fresh_ino

        event = _value_event()
        writer.write_event(event)
        writer.close()

        assert read_all(p) == [event]

    def test_writer_gives_up_after_retry_cap_when_inode_always_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        p = tmp_path / "events.jsonl"
        p.touch()
        real_flock = fcntl.flock
        calls = []

        def flock_always_replacing(fd, operation):
            calls.append(operation)
            p.unlink()
            p.touch()
            return real_flock(fd, operation)

        monkeypatch.setattr(fcntl, "flock", flock_always_replacing)

        with pytest.raises(RuntimeError, match="replaced on every open attempt"):
            TrackingWriter(p)

        assert len(calls) == 5
