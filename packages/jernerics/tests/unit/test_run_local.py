from unittest.mock import patch

from jernerics.config import SweepConfig


class TestLocalBackendPostHook:
    @patch("jernerics.backend.local_backend.sync_artifacts")
    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_calls_sync_after_trials(
        self, mock_cache_dir, mock_run_trial, mock_replay, mock_sync_artifacts, tmp_path
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path

        (tmp_path / "optuna").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "events").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "artifacts").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "heartbeats").mkdir(parents=True)

        backend = LocalBackend(tracking_server="localhost:50051")
        storage_path = str(tmp_path / "optuna" / "mystudy.journal")
        spec = SweepSubmission(
            dag_path=tmp_path / "dag.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=storage_path,
            n_trials=1,
            project_name="proj",
            tracking_dir=tmp_path / "tracking" / "mystudy",
        )
        (tmp_path / "dag.py").write_text("pass")
        (tmp_path / "config.py").write_text(
            "from optuna.samplers import TPESampler\n"
            "base = {}\n"
            "n_trials = 1\n"
            "direction = 'minimize'\n"
            "sampler = TPESampler()\n"
        )

        backend.submit_sweep(spec, direction="minimize")

        mock_replay.assert_called_once()
        mock_sync_artifacts.assert_called_once()

    @patch("jernerics.backend.local_backend.sync_artifacts")
    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_skips_sync_without_tracking_server(
        self, mock_cache_dir, mock_run_trial, mock_replay, mock_sync_artifacts, tmp_path
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path

        (tmp_path / "optuna").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "events").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "artifacts").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "heartbeats").mkdir(parents=True)

        backend = LocalBackend(tracking_server=None)
        storage_path = str(tmp_path / "optuna" / "mystudy.journal")
        spec = SweepSubmission(
            dag_path=tmp_path / "dag.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=storage_path,
            n_trials=1,
            project_name="proj",
            tracking_dir=tmp_path / "tracking" / "mystudy",
        )
        (tmp_path / "dag.py").write_text("pass")
        (tmp_path / "config.py").write_text(
            "from optuna.samplers import TPESampler\n"
            "base = {}\n"
            "n_trials = 1\n"
            "direction = 'minimize'\n"
            "sampler = TPESampler()\n"
        )

        backend.submit_sweep(spec, direction="minimize")

        mock_replay.assert_not_called()
        mock_sync_artifacts.assert_not_called()


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
            backend_overrides={},
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
            backend_overrides={},
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
