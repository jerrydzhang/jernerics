import json
from collections.abc import Iterator
from pathlib import Path
from typing import Self


class TrackingWriter:
    def __init__(self, path: Path):
        self.path = path
        self.file = open(path, "a")  # noqa: SIM115

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def write_envelope(self, envelope: dict) -> None:
        self.file.write(json.dumps(envelope) + "\n")
        self.file.flush()

    def close(self) -> None:
        self.file.close()


class TrackingReader:
    def __init__(self, path: Path):
        self.file = open(path)  # noqa: SIM115

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def __iter__(self) -> Iterator[dict]:
        for line in self.file:
            line = line.strip()
            if line:
                yield json.loads(line)

    def read_envelope(self) -> dict | None:
        line = self.file.readline()
        if not line:
            return None
        return json.loads(line.strip())

    def try_read_envelope(self) -> dict | None:
        pos = self.file.tell()
        line = self.file.readline()
        if not line.strip():
            self.file.seek(pos)
            return None
        try:
            return json.loads(line.strip())
        except json.JSONDecodeError:
            # Partial line (writer mid-flush or crashed); retry later.
            self.file.seek(pos)
            return None

    def close(self) -> None:
        self.file.close()
