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

[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"

[tool.jernerics.backends.hpc.slurm]
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4
""")

    return project_dir


@pytest.fixture
def tmp_trial_config(tmp_project):
    """Create trial and config files in a project directory."""
    (tmp_project / "trial.py").write_text(
        "from jernerics import trial_config, trial_tracker\n"
        "config = trial_config()\n"
        "tracker = trial_tracker()\n"
        'tracker.finish({"done": True})\n'
    )

    (tmp_project / "config.py").write_text("""
base = {"seed": 1}
backend_overrides = {}
""")

    (tmp_project / "uv.lock").write_text("version = 1\n")

    return tmp_project
