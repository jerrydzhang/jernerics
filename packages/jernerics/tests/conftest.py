import pytest


@pytest.fixture
def tmp_project(tmp_path):
    """Create a minimal project directory with pyproject.toml."""
    project_dir = tmp_path / "test-project"
    project_dir.mkdir()

    (project_dir / "pyproject.toml").write_text("""
[project]
name = "test-project"
version = "0.1.0"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"

[tool.jernerics.container]
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4
""")

    return project_dir


@pytest.fixture
def tmp_dag_config(tmp_project):
    """Create DAG and config files in a project directory."""
    (tmp_project / "dag.py").write_text("""
from jernerics.dag import DAG, task

dag = DAG()

@task
def setup(config):
    return {"done": True}
""")

    (tmp_project / "config.py").write_text("""
base = {"seed": 1}
slurm = {}
""")

    (tmp_project / "uv.lock").write_text("version = 1\n")

    return tmp_project
