"""Per-trial artifact manifest: pending blob uploads with a durable cursor.

``{"artifact_id": ..., "key": ..., "path": ...}``. Entries written by
the tracker carry ``"staged": true`` and name Jernerics-owned blob
copies; unmarked paths belong to the caller and are never deleted. The
sidecar ``<manifest>.cursor`` records the byte offset after the last
uploaded entry, so a crashed uploader resumes exactly where it was
acknowledged. Legacy v2 lines (no ``artifact_id``) are skipped: their
artifacts belong to the archived era and are never re-uploaded.
"""

import json
from dataclasses import dataclass
from pathlib import Path


def manifest_cursor_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.name + ".cursor")


@dataclass(frozen=True)
class ManifestEntry:
    artifact_id: str
    key: str
    path: str
    end_offset: int
    staged: bool = False


class ArtifactManifest:
    def __init__(self, path: Path, *, cursor_path: Path | None = None) -> None:
        self.path = path
        self.cursor_path = (
            manifest_cursor_path(path) if cursor_path is None else cursor_path
        )

    def append(
        self, artifact_id: str, key: str, local_path: str, *, staged: bool = False
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry: dict[str, str | bool] = {
            "artifact_id": artifact_id,
            "key": key,
            "path": local_path,
        }
        if staged:
            entry["staged"] = True
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def read_from_cursor(self) -> list[ManifestEntry]:
        if not self.path.exists():
            return []

        offset = self._read_cursor()
        entries: list[ManifestEntry] = []
        with open(self.path, "rb") as f:
            f.seek(offset)
            pos = offset
            for raw in f:
                pos += len(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    break
                artifact_id = data.get("artifact_id")
                if not artifact_id:
                    continue
                entries.append(
                    ManifestEntry(
                        artifact_id=str(artifact_id),
                        key=str(data.get("key", "")),
                        path=str(data.get("path", "")),
                        end_offset=pos,
                        staged=bool(data.get("staged", False)),
                    )
                )
        return entries

    def advance_cursor(self, offset: int) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(str(offset))

    def _read_cursor(self) -> int:
        if not self.cursor_path.exists():
            return 0
        try:
            return int(self.cursor_path.read_text().strip())
        except ValueError:
            return 0
