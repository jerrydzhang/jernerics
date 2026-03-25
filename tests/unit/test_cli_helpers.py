import pytest

from jernerics._cli_helpers import (
    ConfigNotFound,
    ExitCode,
    NoConfigsFound,
    NoContainerFound,
    find_container,
    find_pyproject_dir,
    get_script_path,
    is_tty,
    load_config,
    load_jernerics_config,
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


class TestLoadJernericsConfig:
    def test_load_jernerics_config_basic(self, tmp_project):
        hpc, _shell, _binds = load_jernerics_config(tmp_project)

        assert hpc.host == "user@hpc.example.edu"
        assert hpc.remote_dir == "~/experiments/{project_name}"
        assert hpc.partition == "priority"
        assert hpc.time == "1:00:00"
        assert hpc.mem == "16G"
        assert hpc.cpus == 4

    def test_load_jernerics_config_no_pyproject(self, tmp_path):
        with pytest.raises(ConfigNotFound, match=r"No pyproject.toml found"):
            load_jernerics_config(tmp_path)

    def test_load_jernerics_config_malformed_toml(self, tmp_path):
        project_dir = tmp_path / "malformed"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("invalid [toml content")

        with pytest.raises(ConfigNotFound, match=r"Malformed pyproject.toml"):
            load_jernerics_config(project_dir)

    def test_load_jernerics_config_with_env_host(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "envtest"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "envtest"
version = "0.1.0"
""")

        monkeypatch.setenv("JERNERICS_HPC_HOST", "env@host.example.edu")
        hpc, _shell, _binds = load_jernerics_config(project_dir)

        assert hpc.host == "env@host.example.edu"


class TestFindPyprojectDir:
    def test_find_pyproject_dir_from_project(self, tmp_project):
        result = find_pyproject_dir(tmp_project)
        assert result == tmp_project

    def test_find_pyproject_dir_from_subdir(self, tmp_project):
        subdir = tmp_project / "src" / "mypackage"
        subdir.mkdir(parents=True)

        result = find_pyproject_dir(subdir)
        assert result == tmp_project

    def test_find_pyproject_dir_not_found(self, tmp_path):
        result = find_pyproject_dir(tmp_path)
        assert result is None


class TestFindContainer:
    def test_find_container_explicit(self, tmp_path):
        container = tmp_path / "container.sif"
        container.write_text("")

        result = find_container(str(container), False, str(tmp_path))
        assert result == str(container)

    def test_find_container_explicit_not_found(self, tmp_path):
        with pytest.raises(NoContainerFound, match="Container not found"):
            find_container(str(tmp_path / "missing.sif"), False, str(tmp_path))

    def test_find_container_no_container_flag(self, tmp_path):
        result = find_container(None, True, str(tmp_path))
        assert result is None

    def test_find_container_default_sif(self, tmp_path):
        sif_path = tmp_path / ".jernerics" / "container.sif"
        sif_path.parent.mkdir(parents=True)
        sif_path.write_text("")

        result = find_container(None, False, str(tmp_path))
        assert result == str(sif_path)

    def test_find_container_default_tar(self, tmp_path):
        tar_path = tmp_path / ".jernerics" / "container.tar.gz"
        tar_path.parent.mkdir(parents=True)
        tar_path.write_text("")

        result = find_container(None, False, str(tmp_path))
        assert result == f"docker-archive://{tar_path}"

    def test_find_container_not_found(self, tmp_path):
        with pytest.raises(NoContainerFound, match="No container found"):
            find_container(None, False, str(tmp_path))


class TestLoadConfig:
    def test_load_config_basic(self, tmp_path):
        config_content = """
configs = [
    {"seed": 1, "lr": 0.001},
    {"seed": 2, "lr": 0.01},
]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        slurm, configs, max_workers, executor_type = load_config(str(config_file))

        assert slurm == {}
        assert len(configs) == 2
        assert configs[0]["seed"] == 1
        assert configs[1]["lr"] == 0.01
        assert max_workers is None
        assert executor_type is None

    def test_load_config_with_slurm(self, tmp_path):
        config_content = """
slurm = {
    "time": "1:00:00",
    "mem": "4G",
    "partition": "gpu",
}

configs = [{"seed": 1}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        slurm, configs, _max_workers, _executor_type = load_config(str(config_file))

        assert slurm["time"] == "1:00:00"
        assert slurm["mem"] == "4G"
        assert slurm["partition"] == "gpu"
        assert len(configs) == 1

    def test_load_config_empty_slurm(self, tmp_path):
        config_content = """
configs = [{"x": 1}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        slurm, configs, _max_workers, _executor_type = load_config(str(config_file))

        assert slurm == {}
        assert configs == [{"x": 1}]

    def test_load_config_missing_configs_raises(self, tmp_path):
        config_content = """
slurm = {"time": "1:00:00"}
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        try:
            load_config(str(config_file))
            assert False, "Should have raised NoConfigsFound"
        except NoConfigsFound as e:
            assert "configs" in str(e)

    def test_load_config_empty_configs_raises(self, tmp_path):
        config_content = """
configs = []
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        try:
            load_config(str(config_file))
            assert False, "Should have raised NoConfigsFound"
        except NoConfigsFound:
            pass

    def test_load_config_nonexistent_file(self):
        try:
            load_config("/nonexistent/config.py")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass

    def test_load_config_with_imports(self, tmp_path):
        config_content = """
import os

configs = [{"env": os.environ.get("TEST_VAR", "default")}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, _max_workers, _executor_type = load_config(str(config_file))

        assert "env" in configs[0]

    def test_load_config_with_computed_values(self, tmp_path):
        config_content = """
seeds = [1, 2, 3]
lrs = [0.001, 0.01]

configs = [{"seed": s, "lr": l} for s in seeds for l in lrs]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, _max_workers, _executor_type = load_config(str(config_file))

        assert len(configs) == 6
        assert configs[0] == {"seed": 1, "lr": 0.001}
        assert configs[-1] == {"seed": 3, "lr": 0.01}

    def test_load_config_single_config(self, tmp_path):
        config_content = """
configs = [{"single": "config"}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, _max_workers, _executor_type = load_config(str(config_file))

        assert len(configs) == 1
        assert configs[0] == {"single": "config"}

    def test_load_config_with_nested_data(self, tmp_path):
        config_content = """
configs = [
    {
        "model": {"layers": 3, "hidden": 64},
        "training": {"epochs": 100, "batch": 32},
    }
]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, _max_workers, _executor_type = load_config(str(config_file))

        assert configs[0]["model"]["layers"] == 3
        assert configs[0]["training"]["epochs"] == 100

    def test_load_config_with_special_characters_in_paths(self, tmp_path):
        config_file = tmp_path / "config with spaces.py"
        config_file.write_text('configs = [{"x": 1}]')

        _slurm, configs, _max_workers, _executor_type = load_config(str(config_file))

        assert configs == [{"x": 1}]

    def test_load_config_with_max_workers(self, tmp_path):
        config_content = """
max_workers = 4

configs = [{"seed": 1}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, max_workers, _executor_type = load_config(str(config_file))

        assert max_workers == 4
        assert len(configs) == 1

    def test_load_config_with_executor_type(self, tmp_path):
        config_content = """
executor_type = "serial"

configs = [{"seed": 1}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, _max_workers, executor_type = load_config(str(config_file))

        assert executor_type == "serial"
        assert len(configs) == 1

    def test_load_config_directory_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Config path is not a file"):
            load_config(str(tmp_path))

    def test_load_config_syntax_error(self, tmp_path):
        config_file = tmp_path / "config.py"
        config_file.write_text("this is not valid python [")

        with pytest.raises(RuntimeError, match="Failed to load config file"):
            load_config(str(config_file))
