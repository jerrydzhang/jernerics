import subprocess
from pathlib import Path

from jernerics._cli_helpers import ExitCode


def _create_hpc_project(tmp_path: Path, host: str = "user@hpc.example.edu") -> Path:
    project_dir = tmp_path / "test-project"
    project_dir.mkdir()

    (project_dir / "pyproject.toml").write_text(
        f"""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "{host}"
remote_dir = "~/experiments/{{project_name}}"
"""
    )

    return project_dir


class TestJobsHelp:
    def test_shows_all_option(self):
        result = subprocess.run(
            ["jernerics", "jobs", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--all" in result.stdout
        assert "--json" in result.stdout


class TestJobsRequiresConfig:
    def test_fails_without_pyproject_toml(self, tmp_path):
        result = subprocess.run(
            ["jernerics", "jobs"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR

    def test_fails_without_hpc_host(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path, host="")

        result = subprocess.run(
            ["jernerics", "jobs"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "No HPC host configured" in result.stdout


class TestCancelHelp:
    def test_shows_all_option(self):
        result = subprocess.run(
            ["jernerics", "cancel", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--all" in result.stdout


class TestCancelRequiresConfig:
    def test_fails_without_pyproject_toml(self, tmp_path):
        result = subprocess.run(
            ["jernerics", "cancel", "12345"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR


class TestCancelRequiresJobId:
    def test_fails_without_job_id_or_all(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "cancel"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.GENERAL_ERROR
        assert "Specify a job ID" in result.stdout


class TestLogsHelp:
    def test_shows_follow_and_array_index_options(self):
        result = subprocess.run(
            ["jernerics", "logs", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--follow" in result.stdout
        assert "--array-index" in result.stdout


class TestLogsRequiresConfig:
    def test_fails_without_pyproject_toml(self, tmp_path):
        result = subprocess.run(
            ["jernerics", "logs", "12345"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR


class TestResultsHelp:
    def test_shows_local_dir_option(self):
        result = subprocess.run(
            ["jernerics", "results", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--local-dir" in result.stdout


class TestResultsRequiresConfig:
    def test_fails_without_pyproject_toml(self, tmp_path):
        result = subprocess.run(
            ["jernerics", "results", "12345"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
