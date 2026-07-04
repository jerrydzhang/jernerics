import shutil
import subprocess

import pytest
from jernerics.cli import _capture_git_hash

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


class TestCaptureGitHash:
    @needs_git
    def test_returns_hash_in_git_repo(self, tmp_path):
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        subprocess.run(
            ["git", "config", "user.email", "t@t.test"], cwd=tmp_path, check=True
        )
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        (tmp_path / "f.txt").write_text("x")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)

        h = _capture_git_hash(tmp_path)
        assert h is not None
        assert len(h) == 40

    def test_returns_none_outside_git_repo(self, tmp_path):
        empty = tmp_path / "norepo"
        empty.mkdir()
        assert _capture_git_hash(empty) is None

    def test_returns_none_for_none(self):
        assert _capture_git_hash(None) is None
