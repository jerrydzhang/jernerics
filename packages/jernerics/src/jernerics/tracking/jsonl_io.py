"""JSONL event log IO: one schema event per line plus a durable ship cursor.

The events file is the durable source of truth; every line is a serialized
``TrackingEvent`` model. The sidecar ``<events.jsonl>.cursor`` file records
the byte offset of the last server-acknowledged complete line, so a restarted
shipper resumes exactly where the previous one was acknowledged. Cursor
commits are serialized through a ``.cursor.lock`` sidecar and never regress
while the events file still covers the recorded offset.
"""

import contextlib
import fcntl
import os
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Self

from jernerics_schema import TrackingEvent
from pydantic import BaseModel, TypeAdapter, ValidationError

_EVENT_ADAPTER = TypeAdapter(TrackingEvent)

_OPEN_LOCK_ATTEMPTS = 5


def cursor_path(events_path: Path) -> Path:
    return events_path.with_name(events_path.name + ".cursor")


def cursor_lock_path(events_path: Path) -> Path:
    return Path(str(cursor_path(events_path)) + ".lock")


def read_cursor(events_path: Path) -> int:
    """Byte offset of the last acknowledged complete line; 0 when absent."""
    try:
        return int(cursor_path(events_path).read_text().strip())
    except (OSError, ValueError):
        return 0


def write_cursor(events_path: Path, offset: int) -> None:
    """Durably record the acknowledged byte offset (temp + fsync + rename).

    An exclusive lock on the ``.cursor.lock`` sidecar serializes commits
    across processes, and a unique temp file keeps concurrent commits from
    replacing each other. The cursor never regresses while the events file
    still covers the recorded offset — a stale shipper re-acking an older
    offset leaves the higher cursor in place. A smaller offset is honored
    only when the events file was recreated or truncated below the current
    cursor, so the cursor follows the new file.
    """
    target = cursor_path(events_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(cursor_lock_path(events_path), "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        current = read_cursor(events_path)
        if (
            offset < current
            and events_path.exists()
            and events_path.stat().st_size >= current
        ):
            return
        fd, tmp_name = tempfile.mkstemp(
            dir=target.parent, prefix=f"{target.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(str(offset))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def scan_events(
    path: Path,
    start_offset: int,
    max_events: int | None = None,
) -> tuple[list[tuple[TrackingEvent, int]], int]:
    """Read complete event lines starting at a byte offset.

    Returns the parsed events paired with the byte offset of each line end,
    plus the offset after the last complete line consumed. A trailing partial
    line (writer mid-append or crashed) is not consumed. A cursor beyond the
    current file size means the file was recreated; restart from 0, which the
    server's idempotence makes safe.
    """
    if not path.exists():
        return [], start_offset
    size = path.stat().st_size
    if start_offset > size:
        start_offset = 0

    events: list[tuple[TrackingEvent, int]] = []
    offset = start_offset
    with open(path, "rb") as f:
        f.seek(offset)
        while max_events is None or len(events) < max_events:
            line = f.readline()
            if not line or not line.endswith(b"\n"):
                break
            line_offset = offset
            offset += len(line)
            stripped = line.strip()
            if stripped:
                end = line_offset + len(line)
                events.append((_EVENT_ADAPTER.validate_json(stripped), end))

    return events, offset


class TrackingWriter:
    """Appends one serialized event per line; safe for concurrent writers.

    Acquires a shared ``flock`` via open -> lock -> recheck: after locking,
    the writer verifies the locked inode is still the one linked at
    ``path``. Replay deletes journals only while holding the exclusive
    lock, so a lock acquired on a still-linked inode can no longer be
    unlinked underneath the writer; a mismatch means the inode was deleted
    between ``open`` and ``flock`` and the writer reopens the fresh path.
    The lock is held until ``close()``.
    """

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(_OPEN_LOCK_ATTEMPTS):
            file = open(path, "a")  # noqa: SIM115
            fcntl.flock(file.fileno(), fcntl.LOCK_SH)
            try:
                if os.stat(path).st_ino == os.fstat(file.fileno()).st_ino:
                    self.file = file
                    self._lock = threading.Lock()
                    return
            except FileNotFoundError:
                pass
            file.close()
        raise RuntimeError(
            f"events file {path} was replaced on every open attempt; "
            f"giving up after {_OPEN_LOCK_ATTEMPTS} tries"
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def write_event(self, model: BaseModel) -> None:
        line = model.model_dump_json() + "\n"
        with self._lock:
            self.file.write(line)
            self.file.flush()

    def close(self) -> None:
        with self._lock:
            self.file.close()


class TrackingReader:
    """Iterates validated ``TrackingEvent`` models from a JSONL log."""

    def __init__(self, path: Path):
        self.file = open(path)  # noqa: SIM115

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def __iter__(self) -> Iterator[TrackingEvent]:
        for line in self.file:
            stripped = line.strip()
            if stripped:
                yield _EVENT_ADAPTER.validate_json(stripped)

    def try_read_event(self) -> TrackingEvent | None:
        """Read one event without consuming a partial or malformed line."""
        pos = self.file.tell()
        line = self.file.readline()
        if not line.strip():
            self.file.seek(pos)
            return None
        try:
            return _EVENT_ADAPTER.validate_json(line.strip())
        except ValidationError:
            # Partial line (writer mid-flush or crashed); retry later.
            self.file.seek(pos)
            return None

    def close(self) -> None:
        self.file.close()
