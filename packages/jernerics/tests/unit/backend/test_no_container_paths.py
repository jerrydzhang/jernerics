"""Tests for container_type='none' path resolution."""

from pathlib import Path
from unittest.mock import MagicMock

from jernerics.backend.components.container import NoContainer
from jernerics.backend.components.host import LocalHost
from jernerics.backend.models import SweepSpec
from jernerics.backend.pueue_backend import PueueBackend
from jernerics.backend.slurm_backend import SlurmBackend


class TestPueueNoContainerPaths:
    def _make_backend(self, **overrides):
        defaults = {
            "host": LocalHost(),
            "container": NoContainer(),
            "remote_dir": "/home/user/projects/myproject",
            "cache_dir": "/home/user/.cache/jernerics",
            "tracking_server": None,
            "parallel": 1,
            "syncer": None,
            "heartbeat_interval_s": 60.0,
            "auto_retry": False,
            "stale_after_s": 120,
            "grace_period_s": 120,
            "max_retries": 3,
            "chain_depth_cap": 20,
        }
        defaults.update(overrides)
        return PueueBackend(**defaults)

    def test_work_prefix_uses_remote_dir(self):
        backend = self._make_backend()
        assert backend._work_prefix == "/home/user/projects/myproject"

    def test_cache_prefix_uses_cache_dir(self):
        backend = self._make_backend()
        assert backend._cache_prefix == "/home/user/.cache/jernerics"

    def test_work_prefix_with_container(self):
        container = MagicMock()
        backend = self._make_backend(container=container)
        assert backend._work_prefix == "/work"

    def test_cache_prefix_with_container(self):
        container = MagicMock()
        backend = self._make_backend(container=container)
        assert backend._cache_prefix == "/cache"

    def test_trial_command_uses_host_paths(self):
        backend = self._make_backend()
        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="sqlite:////home/user/.cache/jernerics/optuna/study.db",
            n_trials=3,
            dag_relpath="dag.py",
            config_relpath="config.py",
            project_name="proj",
        )
        script = backend._generate_submit_script(spec)
        assert "/home/user/projects/myproject/dag.py" in script
        assert "/home/user/projects/myproject/config.py" in script
        assert "/home/user/.cache/jernerics/proj/tracking/study" in script
        assert "/work/" not in script

    def test_setup_command_uses_host_paths(self):
        backend = self._make_backend()
        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="sqlite:////home/user/.cache/jernerics/optuna/study.db",
            n_trials=3,
            dag_relpath="dag.py",
            config_relpath="config.py",
        )
        script = backend._generate_submit_script(spec)
        assert "/home/user/projects/myproject/config.py" in script
        assert "/home/user/.cache/jernerics/optuna" in script


class TestSlurmNoContainerPaths:
    def _make_backend(self, **overrides):
        defaults = {
            "host": MagicMock(),
            "container": NoContainer(),
            "syncer": MagicMock(),
            "remote_dir": "/home/user/projects/proj",
            "partition": "priority",
            "time": "1:00:00",
            "mem": "16G",
            "cpus": 4,
            "max_concurrent_jobs": 10,
            "cache_dir": "/home/user/.cache/jernerics",
            "tracking_server": None,
            "heartbeat_interval_s": 60.0,
            "auto_retry": False,
            "stale_after_s": 120,
            "grace_period_s": 120,
            "max_retries": 3,
            "chain_depth_cap": 20,
        }
        defaults.update(overrides)
        return SlurmBackend(**defaults)

    def test_work_prefix_uses_remote_dir(self):
        backend = self._make_backend()
        assert backend._work_prefix == "/home/user/projects/proj"

    def test_cache_prefix_uses_cache_dir(self):
        backend = self._make_backend()
        assert backend._cache_prefix == "/home/user/.cache/jernerics"

    def test_work_prefix_with_container(self):
        container = MagicMock()
        backend = self._make_backend(container=container)
        assert backend._work_prefix == "/work"

    def test_cache_prefix_with_container(self):
        container = MagicMock()
        backend = self._make_backend(container=container)
        assert backend._cache_prefix == "/cache"

    def test_trial_command_uses_host_paths(self):
        backend = self._make_backend()
        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="sqlite:////home/user/.cache/jernerics/optuna/study.db",
            n_trials=3,
            dag_relpath="dag.py",
            config_relpath="config.py",
            project_name="proj",
        )
        script = backend._build_array_script(spec, "minimize")
        assert "/home/user/projects/proj/dag.py" in script
        assert "/home/user/projects/proj/config.py" in script
        assert "/home/user/.cache/jernerics/tracking/study" in script
        assert "/work/" not in script
