import json
from pathlib import Path


class ArtifactManifest:
    def __init__(self, path: Path, *, cursor_path: Path | None = None) -> None:
        self.path = path
        self.cursor_path = cursor_path

    def append(self, key: str, local_path: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({"key": key, "path": local_path})
        with open(self.path, "a") as f:
            f.write(entry + "\n")

    def read_from_cursor(self) -> list[dict]:
        if not self.path.exists():
            return []

        offset = self._read_cursor()
        entries = []
        with open(self.path) as f:
            f.seek(offset)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    break
        return entries

    def advance_cursor(self, offset: int) -> None:
        if self.cursor_path is None:
            return
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(offset))

    def _read_cursor(self) -> int:
        if self.cursor_path is None or not self.cursor_path.exists():
            return 0
        return int(self.cursor_path.read_text().strip())
