import shutil
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from jernerics.backend.project_sync import ProjectSync, _collect_files
from jernerics.sync.exclusions import (
    IGNORE_FILENAME,
    compile_excludes,
    project_excludes,
)


class TestCollectFiles:
    def _spec(self, root):
        return compile_excludes(project_excludes(root))

    def test_collects_project_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")
        (tmp_path / "src" / "utils.py").write_text("utils")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        assert len(files) == 2
        names = [f.name for f in files]
        assert "main.py" in names
        assert "utils.py" in names

    def test_respects_builtin_excludes(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "lib.py").write_text("lib")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        names = [f.name for f in files]
        assert "main.py" in names
        assert "lib.py" not in names

    def test_respects_gitignore(self, tmp_path):
        (tmp_path / ".gitignore").write_text("data/\n")
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "large.csv").write_text("data")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        names = [f.name for f in files]
        assert "main.py" in names
        assert "large.csv" not in names

    def test_respects_jernericsignore_plain_patterns(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("secret.env\ncheckpoints/\n")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")
        (tmp_path / "src" / "secret.env").write_text("secret")
        (tmp_path / "checkpoints").mkdir()
        (tmp_path / "checkpoints" / "model.bin").write_text("model")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        names = [f.name for f in files]
        assert "main.py" in names
        assert "secret.env" not in names
        assert "model.bin" not in names

    def test_respects_rooted_jernericsignore_pattern(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("/scratch.txt\n")
        (tmp_path / "scratch.txt").write_text("root")
        (tmp_path / "nested").mkdir()
        (tmp_path / "nested" / "scratch.txt").write_text("nested")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        names = {str(f.relative_to(tmp_path)) for f in files}
        assert "nested/scratch.txt" in names
        assert "scratch.txt" not in names

    def test_negation_reincludes_gitignore_excluded(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / IGNORE_FILENAME).write_text("!keep.log\n")
        (tmp_path / "keep.log").write_text("kept")
        (tmp_path / "debug.log").write_text("debug")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        names = [f.name for f in files]
        assert "keep.log" in names
        assert "debug.log" not in names

    def test_negation_cannot_reinclude_builtin_excluded(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("!results/\n")
        (tmp_path / "results").mkdir()
        (tmp_path / "results" / "out.json").write_text("{}")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        names = [f.name for f in files]
        assert "out.json" not in names

    def test_jernericsignore_file_itself_collected(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("scratch/\n")

        files = _collect_files(tmp_path, self._spec(tmp_path))

        assert IGNORE_FILENAME in {f.name for f in files}

    @pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
    def test_git_tracked_path_excluded_via_jernericsignore(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "secret.pem").write_text("secret")
        (tmp_path / IGNORE_FILENAME).write_text("data/secret.pem\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

        files = _collect_files(tmp_path, self._spec(tmp_path))

        names = {str(f.relative_to(tmp_path)) for f in files}
        assert "src/main.py" in names
        assert "data/secret.pem" not in names


class TestProjectSync:
    def test_init_sets_attributes(self):
        mock_ssh = MagicMock()
        syncer = ProjectSync(mock_ssh, "~/projects/test")
        assert syncer.host is mock_ssh
        assert syncer.remote_dir == "~/projects/test"

    def test_init_strips_trailing_slash(self):
        mock_ssh = MagicMock()
        syncer = ProjectSync(mock_ssh, "~/projects/test/")
        assert syncer.remote_dir == "~/projects/test"

    def test_container_exists_true(self):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = True
        syncer = ProjectSync(mock_ssh, "~/projects/test")

        result = syncer.container_exists()

        assert result is True
        mock_ssh.file_exists.assert_called_once_with("~/projects/test/container.sif")

    def test_container_exists_false(self):
        mock_ssh = MagicMock()
        mock_ssh.file_exists.return_value = False
        syncer = ProjectSync(mock_ssh, "~/projects/test")

        result = syncer.container_exists()

        assert result is False

    def test_sync_project_respects_gitignore(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = ProjectSync(mock_ssh, "~/projects/test")

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("content")
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "large.csv").write_text("data")
        (tmp_path / ".gitignore").write_text("data/\n*.csv\n")

        with patch("jernerics.backend.project_sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.sync_project(tmp_path, dry_run=True)

            assert result is True

    def test_sync_project_includes_all_by_default(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = ProjectSync(mock_ssh, "~/projects/test")

        (tmp_path / "experiments").mkdir()
        (tmp_path / "experiments" / "trial.py").write_text("trial")
        (tmp_path / "experiments" / "config.py").write_text("config")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")

        with patch("jernerics.backend.project_sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = syncer.sync_project(tmp_path, dry_run=True)

            assert result is True

    def test_sync_project_empty_project(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        syncer = ProjectSync(mock_ssh, "~/projects/test")

        result = syncer.sync_project(tmp_path, dry_run=True)

        assert result is True

    def test_sync_project_actual_sync(self, tmp_path):
        mock_ssh = MagicMock()
        mock_ssh.host = "user@host.example.edu"
        mock_ssh.run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        syncer = ProjectSync(mock_ssh, "~/projects/test")

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")

        with patch("jernerics.backend.project_sync.subprocess.run") as mock_run:
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
        syncer = ProjectSync(mock_ssh, "~/projects/test")

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("main")

        with patch("jernerics.backend.project_sync.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(RuntimeError, match="Failed to extract tar archive"):
                syncer.sync_project(tmp_path, dry_run=False)
