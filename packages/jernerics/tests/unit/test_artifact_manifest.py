from pathlib import Path

from jernerics.tracking.artifact_manifest import ArtifactManifest


class TestAppend:
    def test_appends_json_line(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        ArtifactManifest(manifest_path).append("model.pt", "/work/model.pt")

        lines = manifest_path.read_text().strip().split("\n")
        assert len(lines) == 1
        import json

        entry = json.loads(lines[0])
        assert entry["key"] == "model.pt"
        assert entry["path"] == "/work/model.pt"


class TestReadFromCursor:
    def test_reads_entries_after_cursor(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"

        m = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        m.append("a.pt", "/work/a.pt")
        m.append("b.pt", "/work/b.pt")

        entries = m.read_from_cursor()
        assert len(entries) == 2
        assert entries[0]["key"] == "a.pt"
        assert entries[1]["key"] == "b.pt"

    def test_resumes_from_saved_cursor(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"

        m = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        m.append("a.pt", "/work/a.pt")
        m.append("b.pt", "/work/b.pt")

        # Simulate: first entry was uploaded, cursor advanced
        m.advance_cursor(len(manifest_path.read_text().split("\n")[0]) + 1)

        entries = m.read_from_cursor()
        assert len(entries) == 1
        assert entries[0]["key"] == "b.pt"


class TestCursorFileIO:
    def test_advance_cursor_writes_offset(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"

        m = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        m.append("a.pt", "/work/a.pt")
        first_line_len = len(manifest_path.read_text().split("\n")[0]) + 1
        m.advance_cursor(first_line_len)

        assert cursor_path.read_text().strip() == str(first_line_len)

    def test_cursor_defaults_to_zero(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"

        m = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        m.append("a.pt", "/work/a.pt")

        # Cursor file doesn't exist yet, should start from 0
        entries = m.read_from_cursor()
        assert len(entries) == 1


class TestCrashRecovery:
    def test_discards_incomplete_last_line(self, tmp_path: Path):
        manifest_path = tmp_path / "0.manifest"
        cursor_path = tmp_path / "0.cursor"

        m = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        m.append("a.pt", "/work/a.pt")

        # Simulate crash: append incomplete line
        with open(manifest_path, "a") as f:
            f.write('{"key": "incomplete')

        entries = m.read_from_cursor()
        assert len(entries) == 1
        assert entries[0]["key"] == "a.pt"
