import optuna
import pytest
from jernerics.config import (
    ApptainerConfig,
    BackendConfig,
    ConfigNotFound,
    ExitCode,
    PueueConfig,
    SharedConfig,
    SlurmConfig,
    SweepConfig,
    find_pyproject_dir,
    get_script_path,
    is_tty,
    load_backend_config,
    load_config,
    load_tracking_server,
)


class TestExitCode:
    def test_exit_code_values(self):
        assert ExitCode.SUCCESS == 0
        assert ExitCode.GENERAL_ERROR == 1
        assert ExitCode.SSH_ERROR == 2
        assert ExitCode.CONFIG_ERROR == 3
        assert ExitCode.SLURM_ERROR == 4
        assert ExitCode.CONTAINER_ERROR == 5


class TestIsTty:
    def test_is_tty_returns_bool(self):
        result = is_tty()
        assert isinstance(result, bool)


class TestGetScriptPath:
    def test_get_script_path_existing_script(self):
        path = get_script_path("run_with_container.sh")
        assert path.endswith("run_with_container.sh")

    def test_get_script_path_nonexistent_script(self):
        with pytest.raises(FileNotFoundError, match="Script not found"):
            get_script_path("nonexistent_script.sh")


class TestLoadBackendConfig:
    def test_load_backend_config_basic(self, tmp_project):
        config = load_backend_config("hpc", tmp_project)

        assert config.shared.name == "hpc"
        assert config.shared.type == "slurm"
        assert config.shared.host == "user@hpc.example.edu"
        assert config.shared.remote_dir == "~/experiments/{project_name}"
        assert config.backend is not None
        assert isinstance(config.backend, SlurmConfig)
        assert config.backend.partition == "priority"
        assert config.backend.time == "1:00:00"
        assert config.backend.mem == "16G"
        assert config.backend.cpus == 4

    def test_load_backend_config_not_found(self, tmp_project):
        with pytest.raises(ConfigNotFound, match="Backend 'nonexistent' not found"):
            load_backend_config("nonexistent", tmp_project)

    def test_load_backend_config_no_pyproject(self, tmp_path):
        with pytest.raises(ConfigNotFound, match=r"No pyproject.toml found"):
            load_backend_config("hpc", tmp_path)

    def test_load_backend_config_malformed_toml(self, tmp_path):
        project_dir = tmp_path / "malformed"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("invalid [toml content")

        with pytest.raises(ConfigNotFound, match=r"Malformed pyproject.toml"):
            load_backend_config("hpc", project_dir)

    def test_load_backend_config_with_env_host(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "envtest"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "envtest"
version = "0.1.0"

[tool.jernerics.backends.myhost]
type = "slurm"
host = "original@host.example.edu"
""")

        monkeypatch.setenv("JERNERICS_HPC_HOST", "env@host.example.edu")
        config = load_backend_config("myhost", project_dir)

        assert config.shared.host == "env@host.example.edu"

    def test_load_backend_config_no_backends(self, tmp_path):
        project_dir = tmp_path / "empty"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "empty"
version = "0.1.0"
""")
        with pytest.raises(ConfigNotFound, match="No backends configured"):
            load_backend_config("hpc", project_dir)

    def test_load_backend_config_cache_dir(self, tmp_path):
        project_dir = tmp_path / "cached"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "cached"
version = "0.1.0"

[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@hpc.example.edu"
cache_dir = "/scratch/$USER/jernerics"
""")
        config = load_backend_config("hpc", project_dir)

        assert config.shared.cache_dir == "/scratch/$USER/jernerics"

    def test_load_backend_config_multiple_backends(self, tmp_path):
        project_dir = tmp_path / "multi"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "multi"
version = "0.1.0"

[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@hpc.example.edu"

[tool.jernerics.backends.devbox]
type = "bare"
host = "user@workstation.local"
""")
        hpc = load_backend_config("hpc", project_dir)
        devbox = load_backend_config("devbox", project_dir)

        assert hpc.shared.type == "slurm"
        assert hpc.shared.host == "user@hpc.example.edu"
        assert devbox.shared.type == "bare"
        assert devbox.shared.host == "user@workstation.local"
        assert devbox.backend is None

    def test_load_backend_config_pueue(self, tmp_path):
        project_dir = tmp_path / "pueue"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "pueue-proj"
version = "0.1.0"

[tool.jernerics.backends.local-pueue]
type = "pueue"
parallel = 4
container_type = "docker"
""")
        config = load_backend_config("local-pueue", project_dir)

        assert config.shared.type == "pueue"
        assert config.shared.host is None
        assert isinstance(config.backend, PueueConfig)
        assert config.backend.parallel == 4
        assert config.shared.container_type == "docker"
        assert config.shared.parallel == 4

    def test_load_backend_config_apptainer_build_dir(self, tmp_path):
        project_dir = tmp_path / "apptainer-cfg"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "apptainer-cfg"
version = "0.1.0"

[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@hpc.example.edu"
container_type = "apptainer"

[tool.jernerics.backends.hpc.apptainer]
build_dir = "/dev/shm/build"
""")
        config = load_backend_config("hpc", project_dir)

        assert isinstance(config.container, ApptainerConfig)
        assert config.container.build_dir == "/dev/shm/build"

    def test_load_backend_config_no_apptainer_section_gives_none(self, tmp_path):
        project_dir = tmp_path / "no-apptainer"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "no-apptainer"
version = "0.1.0"

[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@hpc.example.edu"
container_type = "docker"
""")
        config = load_backend_config("hpc", project_dir)

        assert config.container is None

    def test_load_backend_config_apptainer_no_section_gives_default(self, tmp_path):
        project_dir = tmp_path / "apptainer-default"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "apptainer-default"
version = "0.1.0"

[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@hpc.example.edu"
container_type = "apptainer"
""")
        config = load_backend_config("hpc", project_dir)

        assert isinstance(config.container, ApptainerConfig)
        assert config.container.build_dir is None

    def test_load_backend_config_pueue_defaults(self, tmp_path):
        project_dir = tmp_path / "pueue-defaults"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "pueue-defaults"
version = "0.1.0"

[tool.jernerics.backends.local-pueue]
type = "pueue"
""")
        config = load_backend_config("local-pueue", project_dir)

        assert config.shared.type == "pueue"
        assert config.shared.container_type == "apptainer"
        assert config.shared.parallel == 1
        assert isinstance(config.backend, PueueConfig)
        assert config.backend.parallel == 1


class TestLoadTrackingServer:
    def test_tracking_server_from_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRACKING_SERVER", raising=False)
        project_dir = tmp_path / "tracked"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "tracked"
version = "0.1.0"

[tool.jernerics]
tracking_server = "myhost:50051"

[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@hpc.example.edu"
""")
        assert load_tracking_server(project_dir) == "myhost:50051"

    def test_tracking_server_from_env(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "tracked"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "tracked"
version = "0.1.0"
""")
        monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "envhost:50051")
        assert load_tracking_server(project_dir) == "envhost:50051"

    def test_tracking_server_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRACKING_SERVER", raising=False)
        project_dir = tmp_path / "untracked"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "untracked"
version = "0.1.0"
""")
        assert load_tracking_server(project_dir) is None

    def test_env_overrides_config(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "tracked"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "tracked"
version = "0.1.0"

[tool.jernerics]
tracking_server = "confighost:50051"
""")
        monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "envhost:9999")
        assert load_tracking_server(project_dir) == "envhost:9999"


class TestBackendConfig:
    def test_defaults(self):
        config = BackendConfig(shared=SharedConfig(name="test", type="slurm"))
        assert config.shared.host is None
        assert config.shared.remote_dir == "~/experiments/{project_name}"
        assert config.backend is None

    def test_with_slurm_config(self):
        config = BackendConfig(
            shared=SharedConfig(name="test", type="slurm"),
            backend=SlurmConfig(),
        )
        assert config.backend is not None
        assert isinstance(config.backend, SlurmConfig)
        assert config.backend.partition == "priority"
        assert config.backend.time == "1:00:00"
        assert config.backend.mem == "16G"
        assert config.backend.cpus == 4
        assert config.backend.max_concurrent_jobs == 10
        assert config.shared.parallel == 1
        assert config.shared.cache_dir is None


class TestFindPyprojectDir:
    def test_find_pyproject_dir_from_project(self, tmp_project):
        result = find_pyproject_dir(tmp_project)
        assert result == tmp_project

    def test_find_pyproject_dir_from_subdir(self, tmp_project):
        subdir = tmp_project / "src" / "mypackage"
        subdir.mkdir(parents=True)

        result = find_pyproject_dir(subdir)
        assert result == tmp_project


class TestLoadConfig:
    def test_load_config_sweep(self, tmp_path):
        config_content = """
import optuna
from jernerics.dag.executor import ThreadPoolRunner

base = {"seed": 42, "model": "gpt"}

def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-5, 1e-1, log=True),
        "batch_size": trial.suggest_int("batch_size", 16, 128),
    }

n_trials = 50
sampler = optuna.samplers.TPESampler(seed=42)
objective = lambda results: results["train"].value["loss"]
direction = "minimize"

backend_overrides = {"partition": "gpu"}
runner = ThreadPoolRunner(max_workers=4)
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        sweep = load_config(str(config_file))

        assert isinstance(sweep, SweepConfig)
        assert sweep.base == {"seed": 42, "model": "gpt"}
        assert sweep.search_space is not None
        assert sweep.n_trials == 50
        assert isinstance(sweep.sampler, optuna.samplers.TPESampler)
        assert sweep.objective is not None
        assert sweep.direction == "minimize"
        assert sweep.backend_overrides == {"partition": "gpu"}
        assert sweep.runner is not None

    def test_load_config_single_no_search_space(self, tmp_path):
        config_content = """
base = {"seed": 1, "lr": 0.001}
n_trials = 1
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        sweep = load_config(str(config_file))

        assert sweep.base == {"seed": 1, "lr": 0.001}
        assert sweep.search_space is None
        assert sweep.n_trials == 1
        assert sweep.sampler is None
        assert sweep.objective is None
        assert sweep.direction == "minimize"

    def test_load_config_grid_sampler(self, tmp_path):
        config_content = """
import optuna

base = {}
n_trials = 10
sampler = optuna.samplers.GridSampler({"lr": [0.001, 0.01, 0.1]})
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        sweep = load_config(str(config_file))

        assert isinstance(sweep.sampler, optuna.samplers.GridSampler)

    def test_load_config_runner(self, tmp_path):
        config_content = """
backend_overrides = {"time": "1:00:00", "mem": "4G"}
from jernerics.dag.executor import SyncRunner
runner = SyncRunner()
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        sweep = load_config(str(config_file))

        assert sweep.backend_overrides == {"time": "1:00:00", "mem": "4G"}
        assert sweep.runner is not None

    def test_load_config_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.py")

    def test_load_config_no_base_defaults_empty(self, tmp_path):
        config_content = """
n_trials = 5
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        sweep = load_config(str(config_file))

        assert sweep.base == {}

    def test_load_config_defaults(self, tmp_path):
        config_content = """
pass
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        sweep = load_config(str(config_file))

        assert sweep.base == {}
        assert sweep.search_space is None
        assert sweep.n_trials == 1
        assert sweep.sampler is None
        assert sweep.objective is None
        assert sweep.direction == "minimize"
        assert sweep.backend_overrides == {}
        assert sweep.runner is None

    def test_load_config_with_special_characters_in_paths(self, tmp_path):
        config_file = tmp_path / "config with spaces.py"
        config_file.write_text('base = {"x": 1}')

        sweep = load_config(str(config_file))

        assert sweep.base == {"x": 1}

    def test_load_config_directory_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Config path is not a file"):
            load_config(str(tmp_path))

    def test_load_config_syntax_error(self, tmp_path):
        config_file = tmp_path / "config.py"
        config_file.write_text("this is not valid python [")

        with pytest.raises(RuntimeError, match="Failed to load config file"):
            load_config(str(config_file))
