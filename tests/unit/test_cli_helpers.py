from jernerics._cli_helpers import NoConfigsFound, load_config


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

        slurm, configs, max_workers = load_config(str(config_file))

        assert slurm == {}
        assert len(configs) == 2
        assert configs[0]["seed"] == 1
        assert configs[1]["lr"] == 0.01
        assert max_workers is None

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

        slurm, configs, _max_workers = load_config(str(config_file))

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

        slurm, configs, _max_workers = load_config(str(config_file))

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

        _slurm, configs, _max_workers = load_config(str(config_file))

        assert "env" in configs[0]

    def test_load_config_with_computed_values(self, tmp_path):
        config_content = """
seeds = [1, 2, 3]
lrs = [0.001, 0.01]

configs = [{"seed": s, "lr": l} for s in seeds for l in lrs]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, _max_workers = load_config(str(config_file))

        assert len(configs) == 6
        assert configs[0] == {"seed": 1, "lr": 0.001}
        assert configs[-1] == {"seed": 3, "lr": 0.01}

    def test_load_config_single_config(self, tmp_path):
        config_content = """
configs = [{"single": "config"}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, _max_workers = load_config(str(config_file))

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

        _slurm, configs, _max_workers = load_config(str(config_file))

        assert configs[0]["model"]["layers"] == 3
        assert configs[0]["training"]["epochs"] == 100

    def test_load_config_with_special_characters_in_paths(self, tmp_path):
        config_file = tmp_path / "config with spaces.py"
        config_file.write_text('configs = [{"x": 1}]')

        _slurm, configs, _max_workers = load_config(str(config_file))

        assert configs == [{"x": 1}]

    def test_load_config_with_max_workers(self, tmp_path):
        config_content = """
max_workers = 4

configs = [{"seed": 1}]
"""
        config_file = tmp_path / "config.py"
        config_file.write_text(config_content)

        _slurm, configs, max_workers = load_config(str(config_file))

        assert max_workers == 4
        assert len(configs) == 1
