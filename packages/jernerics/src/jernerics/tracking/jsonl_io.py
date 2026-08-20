"""JSONL event log IO: one schema event per line plus a durable ship cursor.

The events file is the durable source of truth; every line is a serialized
``TrackingEvent`` model. The sidecar ``<events.jsonl>.cursor`` file records
the byte offset of the last server-acknowledged complete line, so a restarted
shipper resumes exactly where the previous one was acknowledged.
"""

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Self

from jernerics_schema import TrackingEvent
from pydantic import BaseModel, TypeAdapter, ValidationError

_EVENT_ADAPTER = TypeAdapter(TrackingEvent)


def cursor_path(events_path: Path) -> Path:
    return events_path.with_name(events_path.name + ".cursor")


def read_cursor(events_path: Path) -> int:
    """Byte offset of the last acknowledged complete line; 0 when absent."""
    try:
        return int(cursor_path(events_path).read_text().strip())
    except (OSError, ValueError):
        return 0


def write_cursor(events_path: Path, offset: int) -> None:
    """Durably record the acknowledged byte offset (temp + fsync + rename)."""
    target = cursor_path(events_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(str(offset))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
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
    """Appends one serialized event per line; safe for concurrent writers."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a")  # noqa: SIM115
        self._lock = threading.Lock()

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
