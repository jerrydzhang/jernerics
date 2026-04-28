from unittest.mock import patch

from jernerics.config import SweepConfig


class TestRunLocalSingleConfig:
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    @patch("jernerics.cli.load_config")
    def test_single_config_runs_once(self, mock_load, mock_cache_dir, mock_run_trial):
        import tempfile
        from pathlib import Path

        from jernerics.cli import run_local

        mock_load.return_value = SweepConfig(
            base={"seed": 42},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
        )
        mock_cache_dir.return_value = Path(tempfile.mkdtemp())

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            dag_path = f.name

        try:
            run_local(dag_path, dag_path)
        finally:
            import os

            os.unlink(dag_path)

        assert mock_run_trial.call_count == 1


class TestRunLocalSweep:
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    @patch("jernerics.cli.load_config")
    def test_sweep_runs_n_trials(self, mock_load, mock_cache_dir, mock_run_trial):
        import tempfile
        from pathlib import Path

        from jernerics.cli import run_local

        mock_load.return_value = SweepConfig(
            base={"seed": 42},
            search_space=lambda trial: {"lr": trial.suggest_float("lr", 0.001, 0.1)},
            n_trials=5,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
        )
        mock_cache_dir.return_value = Path(tempfile.mkdtemp())

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            dag_path = f.name

        try:
            run_local(dag_path, dag_path)
        finally:
            import os

            os.unlink(dag_path)

        assert mock_run_trial.call_count == 5
