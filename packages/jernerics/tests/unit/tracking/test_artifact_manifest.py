import json
from pathlib import Path

from jernerics.tracking.artifact_manifest import (
    ArtifactManifest,
    ManifestEntry,
    manifest_cursor_path,
)

ARTIFACT_ID = "a" * 32


class TestAppend:
    def test_appends_json_line_with_artifact_id(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        ArtifactManifest(manifest_path).append(ARTIFACT_ID, "model.pt", "/work/m.pt")

        entry = json.loads(manifest_path.read_text().strip())
        assert entry == {
            "artifact_id": ARTIFACT_ID,
            "key": "model.pt",
            "path": "/work/m.pt",
        }


class TestReadFromCursor:
    def test_yields_typed_entries_with_end_offsets(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        m = ArtifactManifest(manifest_path)
        m.append(ARTIFACT_ID, "a.pt", "/work/a.pt")
        m.append("b" * 32, "b.pt", "/work/b.pt")

        entries = m.read_from_cursor()
        first_line, second_line = manifest_path.read_text().splitlines()

        assert entries[0] == ManifestEntry(
            ARTIFACT_ID, "a.pt", "/work/a.pt", len(first_line) + 1
        )
        assert entries[1] == ManifestEntry(
            "b" * 32, "b.pt", "/work/b.pt", len(first_line) + len(second_line) + 2
        )
        assert manifest_path.read_bytes()[: entries[1].end_offset] == (
            manifest_path.read_bytes()
        )

    def test_resumes_from_saved_cursor(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        m = ArtifactManifest(manifest_path)
        m.append(ARTIFACT_ID, "a.pt", "/work/a.pt")
        m.append("b" * 32, "b.pt", "/work/b.pt")

        m.advance_cursor(m.read_from_cursor()[0].end_offset)

        entries = m.read_from_cursor()
        assert [entry.key for entry in entries] == ["b.pt"]

    def test_legacy_line_without_artifact_id_is_skipped(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        with open(manifest_path, "w") as f:
            f.write(json.dumps({"key": "legacy.pt", "path": "/old/legacy.pt"}) + "\n")
        m = ArtifactManifest(manifest_path)
        m.append(ARTIFACT_ID, "new.pt", "/work/new.pt")

        entries = m.read_from_cursor()

        assert [entry.artifact_id for entry in entries] == [ARTIFACT_ID]


class TestCursorFileIO:
    def test_advance_cursor_writes_offset(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        m = ArtifactManifest(manifest_path)
        m.append(ARTIFACT_ID, "a.pt", "/work/a.pt")
        first_end = m.read_from_cursor()[0].end_offset

        m.advance_cursor(first_end)

        assert manifest_cursor_path(manifest_path).read_text().strip() == str(first_end)

    def test_cursor_defaults_to_zero(self, tmp_path: Path):
        m = ArtifactManifest(tmp_path / "0.manifest")
        m.append(ARTIFACT_ID, "a.pt", "/work/a.pt")

        assert len(m.read_from_cursor()) == 1

    def test_corrupt_cursor_treated_as_zero(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        m = ArtifactManifest(manifest_path)
        m.append(ARTIFACT_ID, "a.pt", "/work/a.pt")
        manifest_cursor_path(manifest_path).write_text("not-a-number")

        assert len(m.read_from_cursor()) == 1


class TestCrashRecovery:
    def test_discards_incomplete_last_line(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        m = ArtifactManifest(manifest_path)
        m.append(ARTIFACT_ID, "a.pt", "/work/a.pt")
        with open(manifest_path, "a") as f:
            f.write('{"artifact_id": "incomplete')

        entries = m.read_from_cursor()

        assert [entry.artifact_id for entry in entries] == [ARTIFACT_ID]
