from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jernerics.hpc.sync import FileSyncer


class TestFileSyncer:
    def test_init_sets_attributes(self):
        mock_ssh = MagicMock()
        syncer = FileSyncer(mock_ssh, "~/projects/test")
        assert syncer.ssh is mock_ssh
        assert syncer.remote_dir == "~/projects/test"

    def test_container_exists_true(self):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = True
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        result = syncer.container_exists()

        assert result is True
        mock_ssh.file_exists.assert_called_once_with("~/projects/test/container.sif")

    def test_container_exists_false(self):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = False
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        result = syncer.container_exists()

        assert result is False

    def test_container_needs_rebuild_no_container(self):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = False
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        result = syncer.container_needs_rebuild("/path/to/uv.lock")

        assert result is True

    def test_container_needs_rebuild_no_mtime(self):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = True
        mock_ssh.getmtime.return_value = None
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        result = syncer.container_needs_rebuild("/path/to/uv.lock")

        assert result is True

    def test_container_needs_rebuild_lock_newer(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = True
        mock_ssh.getmtime.return_value = 1000.0
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_mtime=2000.0)
            result = syncer.container_needs_rebuild(lock_file)

        assert result is True

    def test_container_needs_rebuild_container_newer(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = True
        mock_ssh.getmtime.return_value = 2000.0
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        lock_file = tmp_path / "uv.lock"
        lock_file.write_text("version = 1")

        with patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_mtime=1000.0)
            result = syncer.container_needs_rebuild(lock_file)

        assert result is False

    def test_sync_file_success(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        local_file = tmp_path / "test.txt"
        local_file.write_text("test content")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.sync_file(local_file)

            assert result is True
            mock_run.assert_called_once()
            args, _kwargs = mock_run.call_args
            assert args[0][0] == "scp"
            assert str(local_file) in args[0][1]
            assert "user@host.example.edu:~/projects/test/test.txt" in args[0][2]

    def test_sync_file_not_found(self):
        mock_ssh = MagicMock()
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        with pytest.raises(FileNotFoundError):
            syncer.sync_file("/nonexistent/file.txt")

    def test_sync_file_custom_remote_path(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        local_file = tmp_path / "test.txt"
        local_file.write_text("test content")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.sync_file(local_file, "~/custom/path.txt")

            assert result is True
            args, _kwargs = mock_run.call_args
            assert "user@host.example.edu:~/custom/path.txt" in args[0][2]

    def test_download_file_success(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.download_file(
                "~/projects/test/results.json", tmp_path / "results.json"
            )

            assert result is True
            args, _kwargs = mock_run.call_args
            assert args[0][0] == "scp"
            assert "user@host.example.edu:~/projects/test/results.json" in args[0][1]

    def test_sync_project_respects_gitignore(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("content")
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "large.csv").write_text("data")
        (tmp_path / ".gitignore").write_text("data/\n*.csv\n")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.sync_project(tmp_path, dry_run=True)

            assert result is True

    def test_sync_project_includes_all_by_default(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        (tmp_path / "experiments").mkdir()
        (tmp_path / "experiments" / "dag.py").write_text("dag")
        (tmp_path / "experiments" / "config.py").write_text("config")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.sync_project(tmp_path, dry_run=True)

            assert result is True
