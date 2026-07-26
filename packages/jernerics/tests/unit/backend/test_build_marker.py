import hashlib
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
        host.getmtime.return_value = 1000.0

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")

        assert needs_rebuild(host, "/cache/.build_marker", lock_file) is True

    def test_marker_newer_than_lock_means_no_rebuild(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        host.getmtime.return_value = local_mtime + 100

        assert needs_rebuild(host, "/cache/.build_marker", lock_file) is False

    def test_container_def_hash_mismatch_means_needs_rebuild(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        container_def = tmp_path / "container.def"
        container_def.write_text("Bootstrap: docker\n")

        host.getmtime.return_value = local_mtime + 100
        host.read_file.return_value = "stale_hash_value"

        assert (
            needs_rebuild(
                host,
                "/cache/.build_marker",
                lock_file,
                container_def,
            )
            is True
        )

    def test_container_def_hash_match_means_no_rebuild(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        container_def = tmp_path / "container.def"
        container_def.write_text("Bootstrap: docker\n")

        current_hash = hashlib.sha256(container_def.read_bytes()).hexdigest()

        host.getmtime.return_value = local_mtime + 100
        host.read_file.return_value = current_hash

        assert (
            needs_rebuild(
                host,
                "/cache/.build_marker",
                lock_file,
                container_def,
            )
            is False
        )

    def test_no_container_def_path_skips_hash_check(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        host.getmtime.return_value = local_mtime + 100

        assert needs_rebuild(host, "/cache/.build_marker", lock_file) is False
        host.read_file.assert_not_called()

    def test_nonexistent_container_def_path_skips_hash_check(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        host.getmtime.return_value = local_mtime + 100

        assert (
            needs_rebuild(
                host,
                "/cache/.build_marker",
                lock_file,
                tmp_path / "missing.def",
            )
            is False
        )
        host.read_file.assert_not_called()

    def test_empty_remote_marker_means_rebuild_with_def(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        container_def = tmp_path / "container.def"
        container_def.write_text("Bootstrap: docker\n")

        host.getmtime.return_value = local_mtime + 100
        host.read_file.return_value = ""

        assert (
            needs_rebuild(
                host,
                "/cache/.build_marker",
                lock_file,
                container_def,
            )
            is True
        )

    def test_none_remote_marker_means_rebuild_with_def(self, tmp_path):
        host = MagicMock()

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")
        local_mtime = lock_file.stat().st_mtime

        container_def = tmp_path / "container.def"
        container_def.write_text("Bootstrap: docker\n")

        host.getmtime.return_value = local_mtime + 100
        host.read_file.return_value = None

        assert (
            needs_rebuild(
                host,
                "/cache/.build_marker",
                lock_file,
                container_def,
            )
            is True
        )


class TestWriteMarker:
    def test_writes_marker_file(self):
        host = MagicMock()
        write_marker(host, "/cache/.build_marker")
        host.write_file.assert_called_once_with("/cache/.build_marker", "")

    def test_writes_marker_file_no_container_def(self):
        host = MagicMock()
        write_marker(host, "/cache/.build_marker", None)
        host.write_file.assert_called_once_with("/cache/.build_marker", "")

    def test_writes_marker_file_nonexistent_container_def(self, tmp_path):
        host = MagicMock()
        write_marker(host, "/cache/.build_marker", tmp_path / "missing.def")
        host.write_file.assert_called_once_with("/cache/.build_marker", "")

    def test_writes_hash_to_marker_file(self, tmp_path):
        host = MagicMock()

        container_def = tmp_path / "container.def"
        container_def.write_text("Bootstrap: docker\n")

        expected_hash = hashlib.sha256(container_def.read_bytes()).hexdigest()

        write_marker(host, "/cache/.build_marker", container_def)
        host.write_file.assert_called_once_with("/cache/.build_marker", expected_hash)
