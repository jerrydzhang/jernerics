"""Tests for backend.sync() method."""

from unittest.mock import MagicMock

import pytest
from jernerics.backend.pueue_backend import PueueBackend
from jernerics.backend.slurm_backend import SlurmBackend


def _make_slurm_backend(**overrides):
    defaults = {
        "host": MagicMock(),
        "container": MagicMock(),
        "syncer": MagicMock(),
        "remote_dir": "$HOME/projects/proj",
        "partition": "priority",
        "time": "1:00:00",
        "mem": "16G",
        "cpus": 4,
        "max_concurrent_jobs": 10,
        "cache_dir": None,
        "tracking_server": "myhost:50051",
        "heartbeat_interval_s": 60.0,
        "auto_retry": False,
        "stale_after_s": 120,
        "grace_period_s": 120,
        "max_retries": 3,
        "chain_depth_cap": 20,
        "build_dir": None,
        "project_name": "",
    }
    defaults.update(overrides)
    defaults["container"].wrap = lambda cmd, binds: f"wrapped({cmd})"
    return SlurmBackend(**defaults)


def _make_pueue_backend(**overrides):
    defaults = {
        "host": MagicMock(),
        "container": MagicMock(),
        "remote_dir": "$HOME/projects/proj",
        "cache_dir": "$HOME/.cache/jernerics",
        "tracking_server": "myhost:50051",
        "parallel": 1,
        "syncer": MagicMock(),
        "heartbeat_interval_s": 60.0,
        "auto_retry": False,
        "stale_after_s": 120,
        "grace_period_s": 120,
        "max_retries": 3,
        "chain_depth_cap": 20,
        "build_dir": None,
        "project_name": "",
    }
    defaults.update(overrides)
    defaults["container"].wrap = lambda cmd, binds: f"wrapped({cmd})"
    return PueueBackend(**defaults)


class TestSlurmSync:
    def test_wraps_replay_runner_via_container(self, capsys):
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        host.run.return_value = MagicMock(
            returncode=0, stdout="replayed 5 records", stderr=""
        )
        backend = _make_slurm_backend(host=host)

        backend.sync("myproject")

        host.run.assert_called_once()
        cmd = host.run.call_args[0][0][0]
        assert "cd $HOME/projects/proj" in cmd
        assert "wrapped(python -m jernerics.tracking.replay_runner" in cmd
        assert "--tracking-dir /cache/tracking" in cmd
        assert "--server-addr myhost:50051" in cmd
        output = capsys.readouterr().out
        assert "Sync complete" in output

    def test_passes_study_filter(self):
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        host.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        backend = _make_slurm_backend(host=host)

        backend.sync("myproject", study="my_study")

        cmd = host.run.call_args[0][0][0]
        assert "--study my_study" in cmd

    def test_quotes_study_with_special_chars(self):
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        host.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        backend = _make_slurm_backend(host=host)

        backend.sync("myproject", study="my study")

        cmd = host.run.call_args[0][0][0]
        assert "--study 'my study'" in cmd

    def test_raises_without_tracking_server(self):
        backend = _make_slurm_backend(tracking_server=None)
        with pytest.raises(RuntimeError, match="No tracking server"):
            backend.sync("myproject")

    def test_raises_on_failure(self):
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        host.run.return_value = MagicMock(
            returncode=1, stdout="", stderr="connection refused"
        )
        backend = _make_slurm_backend(host=host)

        with pytest.raises(RuntimeError, match="Sync failed"):
            backend.sync("myproject")


class TestPueueSync:
    def test_wraps_replay_runner_via_container(self, capsys):
        host = MagicMock()
        host.host = "user@devbox.local"
        host.run.return_value = MagicMock(
            returncode=0, stdout="replayed 3 records", stderr=""
        )
        backend = _make_pueue_backend(host=host)

        backend.sync("myproject")

        host.run.assert_called_once()
        cmd = host.run.call_args[0][0][0]
        assert "cd $HOME/projects/proj" in cmd
        assert "wrapped(python -m jernerics.tracking.replay_runner" in cmd
        assert "--server-addr myhost:50051" in cmd
        output = capsys.readouterr().out
        assert "Sync complete" in output

    def test_passes_study_filter(self):
        host = MagicMock()
        host.host = "user@devbox.local"
        host.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        backend = _make_pueue_backend(host=host)

        backend.sync("myproject", study="my_study")

        cmd = host.run.call_args[0][0][0]
        assert "--study my_study" in cmd

    def test_raises_without_tracking_server(self):
        backend = _make_pueue_backend(tracking_server=None)
        with pytest.raises(RuntimeError, match="No tracking server"):
            backend.sync("myproject")

    def test_raises_on_failure(self):
        host = MagicMock()
        host.host = "user@devbox.local"
        host.run.return_value = MagicMock(
            returncode=1, stdout="", stderr="connection refused"
        )
        backend = _make_pueue_backend(host=host)

        with pytest.raises(RuntimeError, match="Sync failed"):
            backend.sync("myproject")
