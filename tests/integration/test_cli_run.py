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

    def test_rejects_set_option_with_empty_key(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)
        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--set", "=value"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == ExitCode.CONFIG_ERROR
        assert "Empty key" in result.stdout


class TestRunSlurmPathHandling:
    def test_project_name_from_current_dir(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"
"""
        )
        (project_dir / "dag.py").write_text(
            "from jernerics.dag import DAG\ndag = DAG()"
        )
        (project_dir / "config.py").write_text("configs = [{'seed': 1}]\nslurm = {}")

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "~/experiments/test-project" in result.stdout

    def test_uses_resolved_project_name_not_empty_string(self, tmp_path):
        project_dir = tmp_path / "my-real-project"
        project_dir.mkdir()

        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "my-real-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"
"""
        )
        (project_dir / "dag.py").write_text(
            "from jernerics.dag import DAG\ndag = DAG()"
        )
        (project_dir / "config.py").write_text("configs = [{'seed': 1}]\nslurm = {}")

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "experiments/my-real-project" in result.stdout
        assert "experiments/" not in result.stdout.replace(
            "experiments/my-real-project", ""
        )

    def test_remote_dir_no_double_slashes(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}/"
"""
        )
        (project_dir / "dag.py").write_text(
            "from jernerics.dag import DAG\ndag = DAG()"
        )
        (project_dir / "config.py").write_text("configs = [{'seed': 1}]\nslurm = {}")

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "//" not in result.stdout


class TestRunSlurmScriptGeneration:
    def test_script_uses_cd_for_shell_expansion(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "REMOTE_DIR=$(cd . && pwd)" in result.stdout
        assert '"${REMOTE_DIR}:/work"' in result.stdout

    def test_script_includes_mkdir_for_log_dir(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "mkdir -p .jernerics/logs" in result.stdout

    def test_output_paths_are_relative(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--output=.jernerics/logs/%A_%a.out" in result.stdout

    def test_no_tilde_in_sbatch_directives(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        lines = result.stdout.split("\n")
        sbatch_lines = [l for l in lines if l.startswith("#SBATCH")]
        for line in sbatch_lines:
            assert "~" not in line

    def test_script_changes_to_remote_dir_before_sbatch_content(self, tmp_path):
        project_dir = _create_hpc_project(tmp_path)

        result = subprocess.run(
            ["jernerics", "run", "slurm", "dag.py", "config.py", "--dry-run"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "cd ~/experiments/test-project" in result.stdout


class TestRunSlurmSubdirectoryPaths:
    def test_config_in_subdirectory_preserves_relative_path(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"
"""
        )

        configs_dir = project_dir / "configs"
        configs_dir.mkdir()

        (project_dir / "dag.py").write_text(
            "from jernerics.dag import DAG\ndag = DAG()"
        )
        (configs_dir / "experiment.py").write_text(
            "configs = [{'seed': 1}]\nslurm = {}"
        )
        (project_dir / "uv.lock").write_text("version = 1\n")

        result = subprocess.run(
            [
                "jernerics",
                "run",
                "slurm",
                "dag.py",
                "configs/experiment.py",
                "--dry-run",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "JERNERICS_CONFIG_FILE=/work/configs/experiment.py" in result.stdout

    def test_dag_in_subdirectory_preserves_relative_path(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"
"""
        )

        experiments_dir = project_dir / "experiments"
        experiments_dir.mkdir()

        (experiments_dir / "pipeline.py").write_text(
            "from jernerics.dag import DAG\ndag = DAG()"
        )
        (project_dir / "config.py").write_text("configs = [{'seed': 1}]\nslurm = {}")
        (project_dir / "uv.lock").write_text("version = 1\n")

        result = subprocess.run(
            [
                "jernerics",
                "run",
                "slurm",
                "experiments/pipeline.py",
                "config.py",
                "--dry-run",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "JERNERICS_DAG_FILE=/work/experiments/pipeline.py" in result.stdout

    def test_both_files_in_subdirectories(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"
"""
        )

        experiments_dir = project_dir / "experiments"
        experiments_dir.mkdir()
        configs_dir = experiments_dir / "configs"
        configs_dir.mkdir()

        (experiments_dir / "pipeline.py").write_text(
            "from jernerics.dag import DAG\ndag = DAG()"
        )
        (configs_dir / "experiment.py").write_text(
            "configs = [{'seed': 1}]\nslurm = {}"
        )
        (project_dir / "uv.lock").write_text("version = 1\n")

        result = subprocess.run(
            [
                "jernerics",
                "run",
                "slurm",
                "experiments/pipeline.py",
                "experiments/configs/experiment.py",
                "--dry-run",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "JERNERICS_DAG_FILE=/work/experiments/pipeline.py" in result.stdout
        assert (
            "JERNERICS_CONFIG_FILE=/work/experiments/configs/experiment.py"
            in result.stdout
        )

    def test_rejects_path_traversal_in_dag_path(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()

        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"
"""
        )

        (project_dir / "dag.py").write_text(
            "from jernerics.dag import DAG\ndag = DAG()"
        )
        (project_dir / "config.py").write_text("configs = [{'seed': 1}]\nslurm = {}")
        (project_dir / "uv.lock").write_text("version = 1\n")

        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        (outside_dir / "malicious.py").write_text("print('bad')")

        result = subprocess.run(
            [
                "jernerics",
                "run",
                "slurm",
                "../outside/malicious.py",
                "config.py",
                "--dry-run",
            ],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
