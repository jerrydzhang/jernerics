from __future__ import annotations

from unittest.mock import MagicMock, patch

from jernerics._cli_helpers import SweepConfig


class TestRunLocalSingleConfig:
    @patch("jernerics.cli.subprocess.run")
    @patch("jernerics.cli.load_config")
    def test_single_config_runs_once(self, mock_load, mock_run):
        mock_load.return_value = SweepConfig(
            _base={"seed": 42},
            search_space=None,
            n_trials=1,
            sampler=None,
            objective_task=None,
            objective_metric=None,
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )
        mock_run.return_value = MagicMock(returncode=0)

        import tempfile

        from jernerics.cli import run_local

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            dag_path = f.name

        try:
            run_local(dag_path, dag_path)
        finally:
            import os

            os.unlink(dag_path)

        assert mock_run.call_count == 1


class TestRunLocalSweep:
    @patch("jernerics.cli.subprocess.run")
    @patch("jernerics.cli.load_config")
    def test_sweep_runs_n_trials(self, mock_load, mock_run):
        mock_load.return_value = SweepConfig(
            _base={"seed": 42},
            search_space=lambda trial: {"lr": trial.suggest_float("lr", 0.001, 0.1)},
            n_trials=5,
            sampler=None,
            objective_task="train",
            objective_metric="loss",
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )
        mock_run.return_value = MagicMock(returncode=0)

        import tempfile

        from jernerics.cli import run_local

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            dag_path = f.name

        try:
            run_local(dag_path, dag_path)
        finally:
            import os

            os.unlink(dag_path)

        assert mock_run.call_count == 5
