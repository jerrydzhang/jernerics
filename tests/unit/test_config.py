import pytest

from jernerics._cli_helpers import (
    ConfigNotFound,
    HpcConfig,
    ShellConfig,
    find_pyproject_dir,
    load_jernerics_config,
)
from jernerics.config import merge_configs


class TestMergeConfigs:
    def test_merge_single_override(self):
        base = {"seed": 42, "lr": 0.001}
        overrides = [{"lr": 0.01}]

        result = merge_configs(base, overrides)

        assert result == [{"seed": 42, "lr": 0.01}]

    def test_merge_multiple_overrides(self):
        base = {"seed": 42, "model": "gpt"}
        overrides = [{"seed": 1}, {"seed": 2}, {"seed": 3}]

        result = merge_configs(base, overrides)

        assert result == [
            {"seed": 1, "model": "gpt"},
            {"seed": 2, "model": "gpt"},
            {"seed": 3, "model": "gpt"},
        ]

    def test_merge_empty_overrides(self):
        base = {"seed": 42}
        overrides = []

        result = merge_configs(base, overrides)

        assert result == []

    def test_merge_adds_new_keys(self):
        base = {"seed": 42}
        overrides = [{"lr": 0.001}, {"lr": 0.01, "batch_size": 32}]

        result = merge_configs(base, overrides)

        assert result == [
            {"seed": 42, "lr": 0.001},
            {"seed": 42, "lr": 0.01, "batch_size": 32},
        ]

    def test_merge_empty_base(self):
        base = {}
        overrides = [{"seed": 1}, {"seed": 2}]

        result = merge_configs(base, overrides)

        assert result == [{"seed": 1}, {"seed": 2}]


class TestLoadJernericsConfig:
    def test_load_config_minimal(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"
""")

        hpc_config, shell_config, _binds = load_jernerics_config(tmp_path)
        assert isinstance(hpc_config, HpcConfig)
        assert isinstance(shell_config, ShellConfig)
        assert hpc_config.host is None
        assert hpc_config.remote_dir == "~/experiments/{project_name}"

    def test_load_config_with_hpc_settings(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/projects/{project_name}"
""")

        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.host == "user@hpc.example.edu"
        assert hpc_config.remote_dir == "~/projects/{project_name}"

    def test_load_config_with_cache_dir(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
cache_dir = "/scratch/$USER/jernerics"
""")

        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.cache_dir == "/scratch/$USER/jernerics"

    def test_load_config_cache_dir_defaults_to_none(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"
""")

        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.cache_dir is None

    def test_load_config_with_container_settings(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.container]
partition = "gpu"
time = "2:00:00"
mem = "32G"
cpus = 8
""")

        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.partition == "gpu"
        assert hpc_config.time == "2:00:00"
        assert hpc_config.mem == "32G"
        assert hpc_config.cpus == 8

    def test_load_config_with_safety_settings(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.safety]
max_concurrent_jobs = 5
""")

        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.max_concurrent_jobs == 5

    def test_load_config_from_env(self, tmp_path, monkeypatch):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"
""")

        monkeypatch.setenv("JERNERICS_HPC_HOST", "env@hpc.example.edu")
        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.host == "env@hpc.example.edu"

    def test_load_config_env_overrides_toml(self, tmp_path, monkeypatch):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "toml@hpc.example.edu"
""")

        monkeypatch.setenv("JERNERICS_HPC_HOST", "env@hpc.example.edu")
        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.host == "env@hpc.example.edu"

    def test_load_config_no_pyproject(self, tmp_path):
        with pytest.raises(ConfigNotFound):
            load_jernerics_config(tmp_path / "nonexistent")


class TestShellConfig:
    def test_shell_config_defaults(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"
""")

        _, shell_config, _ = load_jernerics_config(tmp_path)
        assert shell_config.partition is None
        assert shell_config.cpus is None
        assert shell_config.mem is None
        assert shell_config.gpu == 0
        assert shell_config.time is None

    def test_shell_config_from_toml(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.shell]
partition = "gpu"
cpus = 4
mem = "8G"
gpu = 2
time = "2:00:00"
""")

        _, shell_config, _ = load_jernerics_config(tmp_path)
        assert shell_config.partition == "gpu"
        assert shell_config.cpus == 4
        assert shell_config.mem == "8G"
        assert shell_config.gpu == 2
        assert shell_config.time == "2:00:00"


class TestFindPyprojectDir:
    def test_find_in_current_dir(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")

        result = find_pyproject_dir(tmp_path)
        assert result == tmp_path

    def test_find_in_parent(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'test'")
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        result = find_pyproject_dir(subdir)
        assert result == tmp_path

    def test_not_found(self, tmp_path):
        result = find_pyproject_dir(tmp_path)
        assert result is None


class TestConfigEdgeCases:
    def test_remote_path_fallback_to_remote_dir(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
remote_path = "~/custom/path/{project_name}"
""")
        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.remote_dir == "~/custom/path/{project_name}"

    def test_remote_dir_used_when_no_remote_path(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
remote_dir = "~/custom/dir/{project_name}"
""")
        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.remote_dir == "~/custom/dir/{project_name}"

    def test_remote_path_preferred_over_remote_dir(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
remote_path = "~/preferred/{project_name}"
remote_dir = "~/not_preferred/{project_name}"
""")
        hpc_config, _, _ = load_jernerics_config(tmp_path)
        assert hpc_config.remote_dir == "~/preferred/{project_name}"
