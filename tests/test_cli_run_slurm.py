import os
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

[tool.jernerics.container]
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4
"""
    )

    (project_dir / "dag.py").write_text(
        """
from jernerics.dag import DAG, task

dag = DAG()

@task
def setup(config):
    return {"done": True}
"""
    )

    (project_dir / "config.py").write_text(
        """
configs = [
    {"seed": 1},
]

slurm = {
    "partition": "priority",
    "time": "1:00:00",
}
"""
    )

    (project_dir / "uv.lock").write_text("version = 1\n")

    return project_dir


class TestRunSlurmHelp:
    def test_shows_dry_run_and_set_options(self):
        result = subprocess.run(
            ["jernerics", "run", "slurm", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--dry-run" in result.stdout
        assert "--set" in result.stdout


class TestRunSlurmRequiresConfig:
    def test_fails_without_pyproject_toml(self, tmp_path):
        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR

    def test_fails_without_hpc_host(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path, host="")

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "No HPC host configured" in result.stdout


class TestRunSlurmDryRun:
    def test_prints_slurm_script(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "DRY RUN" in result.stdout
        assert "user@hpc.example.edu" in result.stdout
        assert "#!/usr/bin/env bash" in result.stdout
        assert "#SBATCH" in result.stdout
        assert "apptainer exec" in result.stdout

    def test_applies_set_option_overrides(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            [
                "jernerics",
                "run",
                "slurm",
                "dag.py",
                "config.py",
                "--dry-run",
                "--set",
                "mem=32G",
                "--set",
                "time=2:00:00",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--mem=32G" in result.stdout
        assert "--time=2:00:00" in result.stdout

    def test_uses_env_host_over_toml(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path, host="")

        env = os.environ.copy()
        env["JERNERICS_HPC_HOST"] = "env-user@hpc.example.edu"

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        assert "env-user@hpc.example.edu" in result.stdout

    def test_includes_container_sif_path(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "container.sif" in result.stdout

    def test_generates_correct_array_spec(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--array=1-1" in result.stdout

    def test_substitutes_project_name_in_remote_dir(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "~/experiments/test-project" in result.stdout


class TestRunSlurmInvalidOptions:
    def test_rejects_set_option_without_equals(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--set", "invalid"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "Invalid --set option" in result.stdout
