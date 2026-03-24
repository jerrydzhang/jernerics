import subprocess


class TestDAGWorkflow:
    def test_full_workflow_simple(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        dag_file.write_text("""
from jernerics.dag import task, DAG

@task
def prepare(config):
    return f"data_{config['seed']}"

@task(depends_on=[prepare])
def train(prepare, config):
    return f"model_from_{prepare}"

dag = DAG(__file__)
""")

        config_file = tmp_path / "config.py"
        config_file.write_text("""
slurm = {"time": "1:00:00", "mem": "4G"}

configs = [{"seed": 1}, {"seed": 2}]
""")

        result = subprocess.run(
            ["jernerics", "run", "local", str(dag_file), str(config_file)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert "Running config 1/2" in result.stdout
        assert "Running config 2/2" in result.stdout
        assert result.stdout.count("DAG completed") == 2

        state_dir = tmp_path / ".jernerics" / "runs"
        assert state_dir.exists()
        assert (state_dir / "latest_0.json").exists()
        assert (state_dir / "latest_1.json").exists()

    def test_full_workflow_with_failure(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        dag_file.write_text("""
from jernerics.dag import task, DAG

@task
def failing_task(config):
    raise ValueError("intentional error")

@task(depends_on=[failing_task])
def dependent(failing_task, config):
    return "should not run"

dag = DAG(__file__)
""")

        config_file = tmp_path / "config.py"
        config_file.write_text("""
configs = [{"seed": 1}]
""")

        result = subprocess.run(
            ["jernerics", "run", "local", str(dag_file), str(config_file)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

        assert result.returncode == 1
        assert "failed" in result.stdout.lower()

    def test_empty_configs_error(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        dag_file.write_text("""
from jernerics.dag import task, DAG

@task
def my_task(config):
    return 1

dag = DAG(__file__)
""")

        config_file = tmp_path / "config.py"
        config_file.write_text("""
configs = []
""")

        result = subprocess.run(
            ["jernerics", "run", "local", str(dag_file), str(config_file)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

        assert "configs" in result.stdout.lower()

    def test_diamond_dependency(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        dag_file.write_text("""
from jernerics.dag import task, DAG

@task
def a(config):
    return 1

@task(depends_on=[a])
def b(a, config):
    return a + 10

@task(depends_on=[a])
def c(a, config):
    return a + 100

@task(depends_on=[b, c])
def d(b, c, config):
    return b + c

dag = DAG(__file__)
""")

        config_file = tmp_path / "config.py"
        config_file.write_text("""
configs = [{"seed": 1}]
""")

        result = subprocess.run(
            ["jernerics", "run", "local", str(dag_file), str(config_file)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert "DAG completed" in result.stdout

    def test_config_with_spaces_in_path(self, tmp_path):
        dag_file = tmp_path / "my dag.py"
        dag_file.write_text("""
from jernerics.dag import task, DAG

@task
def my_task(config):
    return config["value"]

dag = DAG(__file__)
""")

        config_file = tmp_path / "my config.py"
        config_file.write_text("""
configs = [{"value": 42}]
""")

        result = subprocess.run(
            ["jernerics", "run", "local", str(dag_file), str(config_file)],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert "DAG completed" in result.stdout


class TestSlurmCommandGeneration:
    def test_slurm_command_generation(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")

        dag_file = tmp_path / "dag.py"
        dag_file.write_text("""
from jernerics.dag import task, DAG

@task
def my_task(config):
    return 1

dag = DAG(__file__)
""")

        config_file = tmp_path / "config.py"
        config_file.write_text("""
slurm = {"time": "2:00:00", "mem": "8G", "partition": "gpu"}
configs = [{"seed": 1}, {"seed": 2}, {"seed": 3}]
""")

        result = subprocess.run(
            [
                "jernerics",
                "run",
                "slurm",
                str(dag_file),
                str(config_file),
                "--dry-run",
                "-S",
                "time=4:00:00",
            ],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )

        assert result.returncode == 0
        assert "--array=1-3" in result.stdout
        assert "--time" in result.stdout
        assert "4:00:00" in result.stdout
        assert "--mem" in result.stdout
        assert "8G" in result.stdout
        assert "--partition" in result.stdout
        assert "gpu" in result.stdout
