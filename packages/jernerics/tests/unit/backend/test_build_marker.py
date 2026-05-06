from pathlib import Path
from unittest.mock import MagicMock

from jernerics.backend.build_marker import needs_rebuild, write_marker


class TestNeedsRebuild:
    def test_no_marker_means_needs_rebuild(self):
        host = MagicMock()
        host.getmtime.return_value = None
        assert (
            needs_rebuild(host, "/cache/.build_marker", Path("/local/uv.lock")) is True
        )

    def test_no_local_lock_means_needs_rebuild(self, tmp_path):
        host = MagicMock()
        host.getmtime.return_value = 1000.0
        assert (
            needs_rebuild(host, "/cache/.build_marker", tmp_path / "nonexistent.lock")
            is True
        )

    def test_lock_newer_than_marker_means_needs_rebuild(self, tmp_path):
        host = MagicMock()
        # Marker is old
        host.getmtime.return_value = 1000.0

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")

        # Lock was just written (mtime is ~now), so it's newer than 1000.0
        assert needs_rebuild(host, "/cache/.build_marker", lock_file) is True

    def test_marker_newer_than_lock_means_no_rebuild(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        # Marker is slightly newer than lock
        host.getmtime.return_value = local_mtime + 100

        assert needs_rebuild(host, "/cache/.build_marker", lock_file) is False


class TestWriteMarker:
    def test_writes_marker_file(self):
        host = MagicMock()
        write_marker(host, "/cache/.build_marker")
        host.write_file.assert_called_once_with("/cache/.build_marker", "")
