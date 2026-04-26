from pathlib import Path
from unittest.mock import MagicMock, patch

import pathspec
import pytest
from jernerics.hpc.sync import (
    DEFAULT_EXCLUDES,
    FileSyncer,
    _collect_files,
    _load_gitignore,
    _should_include,
)


class TestLoadGitignore:
    def test_loads_existing_gitignore(self, tmp_path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n__pycache__/\n")

        spec = _load_gitignore(tmp_path)

        assert spec is not None
        assert spec.match_file("test.pyc")
        assert spec.match_file("__pycache__/module.pyc")

    def test_returns_none_when_no_gitignore(self, tmp_path):
        spec = _load_gitignore(tmp_path)
        assert spec is None


class TestShouldInclude:
    def test_includes_normal_file(self):
        default_spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_EXCLUDES)
        result = _should_include("src/main.py", None, default_spec)
        assert result is True

    def test_excludes_by_default_spec(self):
        default_spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_EXCLUDES)
        result = _should_include("results/output.json", None, default_spec)
        assert result is False

    def test_excludes_by_gitignore(self, tmp_path):
        gitignore_spec = pathspec.PathSpec.from_lines("gitignore", ["*.log"])
        default_spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_EXCLUDES)
        result = _should_include("debug.log", gitignore_spec, default_spec)
        assert result is False

    def test_gitignore_overrides_default(self, tmp_path):
        gitignore_spec = pathspec.PathSpec.from_lines("gitignore", ["custom.log"])
        default_spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_EXCLUDES)
        result = _should_include("custom.log", gitignore_spec, default_spec)
        assert result is False


class TestCollectFiles:
    def test_collects_project_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")
        (tmp_path / "src" / "utils.py").write_text("utils")

        default_spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_EXCLUDES)
        files = _collect_files(tmp_path, None, default_spec)

        assert len(files) == 2
        names = [f.name for f in files]
        assert "main.py" in names
        assert "utils.py" in names

    def test_respects_default_excludes(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "lib.py").write_text("lib")

        default_spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_EXCLUDES)
        files = _collect_files(tmp_path, None, default_spec)

        names = [f.name for f in files]
        assert "main.py" in names
        assert "lib.py" not in names


class TestFileSyncer:
    def test_init_sets_attributes(self):
        mock_ssh = MagicMock()
        syncer = FileSyncer(mock_ssh, "~/projects/test")
        assert syncer.ssh is mock_ssh
        assert syncer.remote_dir == "~/projects/test"

    def test_init_strips_trailing_slash(self):
        mock_ssh = MagicMock()
        syncer = FileSyncer(mock_ssh, "~/projects/test/")
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

    def test_container_needs_rebuild_lock_missing(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = True
        mock_ssh.getmtime.return_value = 1000.0
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        result = syncer.container_needs_rebuild(tmp_path / "missing.lock")

        assert result is True

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

    def test_download_file_no_local_path(self):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.download_file("~/projects/test/results.json")

            assert result is True
            args, _kwargs = mock_run.call_args
            assert "results.json" in args[0][2]

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

    def test_sync_project_empty_project(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        result = syncer.sync_project(tmp_path, dry_run=True)

        assert result is True

    def test_sync_project_actual_sync(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        mock_ssh.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.sync_project(tmp_path, dry_run=False)

            assert result is True
            mock_ssh.mkdir.assert_called_once()
            mock_run.assert_called()

    def test_sync_project_tar_failure(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        mock_ssh.run.return_value = MagicMock(
            returncode=1, stderr="tar error", stdout=""
        )
        syncer = FileSyncer(mock_ssh, "~/projects/test")

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")

        with patch("jernerics.hpc.sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(RuntimeError, match="Failed to extract tar archive"):
                syncer.sync_project(tmp_path, dry_run=False)
