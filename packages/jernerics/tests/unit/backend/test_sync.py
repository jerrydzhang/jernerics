import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jernerics.backend.project_sync import (
    MANIFEST_FILENAME,
    ProjectSync,
    _collect_files,
)
from jernerics.sync.exclusions import (
    IGNORE_FILENAME,
    compile_excludes,
    project_excludes,
)

_real_run = subprocess.run


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


class ExecutingHost:
    """Host double that runs commands for real against a local mirror tree."""

    is_local = False

    def __init__(self, root: Path):
        self.host = "mirror@localhost"
        self.home = str(root)
        self.shell_commands: list[str] = []

    def run(self, command, **kwargs):
        if len(command) == 1:
            return _real_run(["sh", "-c", command[0]], **kwargs)
        return _real_run(list(command), **kwargs)

    def shell(self, command, **kwargs):
        self.shell_commands.append(command)
        return _real_run(["sh", "-c", command], **kwargs)

    def mkdir(self, remote_path):
        Path(remote_path).mkdir(parents=True, exist_ok=True)

    def file_exists(self, remote_path):
        return Path(remote_path).is_file()

    def getmtime(self, remote_path):
        path = Path(remote_path)
        return path.stat().st_mtime if path.is_file() else None

    def read_file(self, remote_path):
        path = Path(remote_path)
        return path.read_text() if path.is_file() else None

    def remove_file(self, remote_path):
        path = Path(remote_path)
        if path.is_file():
            path.unlink()

    def write_file(self, remote_path, content):
        Path(remote_path).write_text(content)


def _patched_scp(host: ExecutingHost):
    """Stand in for scp by copying the tarball into the host's mirror tree."""

    def _copy(command, **kwargs):
        local_tar, destination = command[1], command[2]
        assert destination.startswith(f"{host.host}:")
        shutil.copy(local_tar, destination.split(":", 1)[1])
        return subprocess.CompletedProcess(args=list(command), returncode=0)

    return patch(
        "jernerics.backend.project_sync.subprocess.run",
        side_effect=_copy,
    )


class TestSyncManifest:
    def _make(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        remote = tmp_path / "remote"
        host = ExecutingHost(remote)
        return project, remote, ProjectSync(host, str(remote)), host

    def _sync(self, syncer, project):
        with _patched_scp(syncer.host):
            assert syncer.sync_project(project) is True

    def _rm_commands(self, host):
        return [c for c in host.shell_commands if "rm -f --" in c]

    def test_first_sync_issues_no_rm_and_writes_manifest(self, tmp_path):
        project, remote, syncer, host = self._make(tmp_path)
        (project / "A.py").write_text("a1")
        (project / "B.py").write_text("b1")

        self._sync(syncer, project)

        assert (remote / "A.py").read_text() == "a1"
        assert (remote / "B.py").read_text() == "b1"
        assert host.shell_commands == []
        assert (remote / MANIFEST_FILENAME).read_text() == "A.py\nB.py\n"

    def test_second_sync_deletes_locally_removed_file(self, tmp_path):
        project, remote, syncer, host = self._make(tmp_path)
        (project / "A.py").write_text("a1")
        (project / "B.py").write_text("b1")
        self._sync(syncer, project)

        (project / "B.py").unlink()
        (project / "A.py").write_text("a2")
        self._sync(syncer, project)

        assert (remote / "A.py").read_text() == "a2"
        assert not (remote / "B.py").exists()
        rm_commands = self._rm_commands(host)
        assert len(rm_commands) == 1
        assert rm_commands[0].startswith(f"cd {remote} && rm -f -- ")
        assert rm_commands[0].endswith("B.py")
        assert (remote / MANIFEST_FILENAME).read_text() == "A.py\n"

    def test_second_sync_deletes_newly_ignored_file(self, tmp_path):
        project, remote, syncer, host = self._make(tmp_path)
        (project / "A.py").write_text("a1")
        (project / "B.py").write_text("b1")
        self._sync(syncer, project)

        (project / IGNORE_FILENAME).write_text("B.py\n")
        (project / "A.py").write_text("a2")
        self._sync(syncer, project)

        assert (project / "B.py").exists()
        assert not (remote / "B.py").exists()
        assert (remote / "A.py").read_text() == "a2"
        rm_commands = self._rm_commands(host)
        assert len(rm_commands) == 1
        assert rm_commands[0].endswith("B.py")
        expected_manifest = "\n".join(sorted([IGNORE_FILENAME, "A.py"])) + "\n"
        assert (remote / MANIFEST_FILENAME).read_text() == expected_manifest

    def test_unsafe_manifest_entries_skipped(self, tmp_path, capsys):
        project, remote, syncer, host = self._make(tmp_path)
        (project / "A.py").write_text("a1")
        remote.mkdir()
        (remote / "ok_stale.py").write_text("stale")
        (remote / MANIFEST_FILENAME).write_text("../evil\n/etc/passwd\nok_stale.py\n")

        self._sync(syncer, project)

        err = capsys.readouterr().err
        assert "skipped unsafe manifest entry: ../evil" in err
        assert "skipped unsafe manifest entry: /etc/passwd" in err
        assert not (remote / "ok_stale.py").exists()
        assert (remote / "A.py").exists()
        rm_commands = self._rm_commands(host)
        assert len(rm_commands) == 1
        assert "ok_stale.py" in rm_commands[0]
        assert "../evil" not in rm_commands[0]
        assert "/etc/passwd" not in rm_commands[0]

    def test_backend_owned_artifact_survives_syncs(self, tmp_path):
        project, remote, syncer, _ = self._make(tmp_path)
        (project / "A.py").write_text("a1")
        (project / "B.py").write_text("b1")
        remote.mkdir()
        (remote / "container.sif").write_text("image-bytes")

        self._sync(syncer, project)
        (project / "B.py").unlink()
        self._sync(syncer, project)

        assert (remote / "container.sif").read_text() == "image-bytes"
        assert not (remote / "B.py").exists()
        assert syncer.container_exists() is True

    def test_dry_run_reports_deletions_without_executing(self, tmp_path, capsys):
        project, remote, syncer, host = self._make(tmp_path)
        (project / "A.py").write_text("a1")
        remote.mkdir()
        (remote / "B.py").write_text("b1")
        (remote / MANIFEST_FILENAME).write_text("A.py\nB.py\n")

        assert syncer.sync_project(project, dry_run=True) is True

        out = capsys.readouterr().out
        assert "Would sync 1 files" in out
        assert "Would delete 1 stale file(s)" in out
        assert host.shell_commands == []
        assert (remote / "B.py").exists()
        assert (remote / MANIFEST_FILENAME).read_text() == "A.py\nB.py\n"

    def test_deletions_batched_at_100_paths(self, tmp_path):
        project, remote, syncer, host = self._make(tmp_path)
        (project / "A.py").write_text("a1")
        remote.mkdir()
        stale_names = [f"stale_{i:03d}.py" for i in range(150)]
        for name in stale_names:
            (remote / name).write_text("stale")
        (remote / MANIFEST_FILENAME).write_text(
            "\n".join(["A.py", *stale_names]) + "\n"
        )

        self._sync(syncer, project)

        rm_commands = self._rm_commands(host)
        assert len(rm_commands) == 2
        counts = [len(c.split("rm -f -- ", 1)[1].split()) for c in rm_commands]
        assert counts == [100, 50]
        assert not any((remote / name).exists() for name in stale_names)
        assert (remote / "A.py").read_text() == "a1"
