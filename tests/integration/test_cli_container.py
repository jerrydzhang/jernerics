import subprocess

from jernerics._cli_helpers import ExitCode


class TestContainerHelp:
    def test_shows_build_subcommand(self):
        result = subprocess.run(
            ["jernerics", "container", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "build" in result.stdout


class TestContainerBuildHelp:
    def test_shows_force_and_dry_run_options(self):
        result = subprocess.run(
            ["jernerics", "container", "build", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--force" in result.stdout
        assert "--dry-run" in result.stdout


class TestContainerBuildErrors:
    def test_fails_without_pyproject_toml(self, tmp_path):
        result = subprocess.run(
            ["jernerics", "container", "build", str(tmp_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "pyproject.toml" in result.stdout or "pyproject.toml" in result.stderr

    def test_fails_without_uv_lock(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "test"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
"""
        )

        result = subprocess.run(
            ["jernerics", "container", "build", str(tmp_path)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "uv.lock" in result.stdout.lower()
