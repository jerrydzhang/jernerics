from unittest.mock import patch

from jernerics.config import SweepConfig


class TestLocalBackendPostHook:
    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.resolve_tracking_ship")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_calls_replay_after_trials(
        self,
        mock_cache_dir,
        mock_run_trial,
        mock_resolve_tracking_ship,
        mock_replay,
        tmp_path,
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path

        (tmp_path / "optuna").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "events").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "artifacts").mkdir(parents=True)
        (tmp_path / "tracking" / "mystudy" / "heartbeats").mkdir(parents=True)

        mock_resolve_tracking_ship.return_value = (
            "http://localhost:8000",
            None,
        )  # (base_url, api_key)

        backend = LocalBackend(tracking_server="localhost:50051")
        storage_path = str(tmp_path / "optuna" / "mystudy.journal")
        spec = SweepSubmission(
            trial_path=tmp_path / "trial.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=storage_path,
            n_trials=1,
            project_name="proj",
            tracking_dir=tmp_path / "tracking" / "mystudy",
        )
        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text(
            "from optuna.samplers import TPESampler\n"
            "base = {}\n"
            "n_trials = 1\n"
            "direction = 'minimize'\n"
            "sampler = TPESampler()\n"
        )

        backend.submit_sweep(spec, direction="minimize")

        mock_replay.assert_called_once()

    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_skips_sync_without_tracking_server(
        self, mock_cache_dir, mock_run_trial, mock_replay, tmp_path
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
            trial_path=tmp_path / "trial.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=storage_path,
            n_trials=1,
            project_name="proj",
            tracking_dir=tmp_path / "tracking" / "mystudy",
        )
        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text(
            "from optuna.samplers import TPESampler\n"
            "base = {}\n"
            "n_trials = 1\n"
            "direction = 'minimize'\n"
            "sampler = TPESampler()\n"
        )

        backend.submit_sweep(spec, direction="minimize")

        mock_replay.assert_not_called()


class TestLocalBackendSubmissionEvents:
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_emits_submission_events_when_project_named(
        self, mock_cache_dir, mock_run_trial, tmp_path
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path
        (tmp_path / "optuna").mkdir(parents=True)
        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text(
            "from optuna.samplers import TPESampler\n"
            "base = {}\n"
            "n_trials = 1\n"
            "direction = 'minimize'\n"
            "sampler = TPESampler()\n"
        )

        spec = SweepSubmission(
            trial_path=tmp_path / "trial.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=str(tmp_path / "optuna" / "mystudy.journal"),
            n_trials=1,
            project_name="proj",
            tracking_dir=tmp_path / "tracking" / "mystudy",
            git_hash="abc123",
        )

        LocalBackend(tracking_server=None).submit_sweep(spec)

        path = (
            tmp_path
            / "tracking"
            / "mystudy"
            / "submission"
            / f"{spec.submission_id}.jsonl"
        )
        events = _read_submission_events(path)
        assert [event.tag for event in events] == [
            "sweep_snapshot",
            "submission_snapshot",
            "job_snapshot",
        ]
        submission = events[1]
        assert submission.expected_trials == 1
        assert submission.git_hash == "abc123"
        assert submission.config_source == str(tmp_path / "config.py")
        assert events[2].role == "trials"
        assert events[2].scheduler_job_id == "local"

    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_skips_emission_without_project_name(
        self, mock_cache_dir, mock_run_trial, tmp_path
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path
        (tmp_path / "optuna").mkdir(parents=True)
        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text(
            "from optuna.samplers import TPESampler\n"
            "base = {}\n"
            "n_trials = 1\n"
            "direction = 'minimize'\n"
            "sampler = TPESampler()\n"
        )

        spec = SweepSubmission(
            trial_path=tmp_path / "trial.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=str(tmp_path / "optuna" / "mystudy.journal"),
            n_trials=1,
            tracking_dir=tmp_path / "tracking" / "mystudy",
        )

        LocalBackend(tracking_server=None).submit_sweep(spec)

        assert not (tmp_path / "tracking" / "mystudy" / "submission").exists()


def _read_submission_events(path):
    from jernerics.tracking.jsonl_io import TrackingReader

    with TrackingReader(path) as reader:
        return list(reader)


class TestRunLocalSingleConfig:
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    @patch("jernerics.commands.execution.load_config")
    def test_single_config_runs_once(self, mock_load, mock_cache_dir, mock_run_trial):
        import tempfile
        from pathlib import Path

        from jernerics.commands.execution import run_local

        mock_load.return_value = SweepConfig(
            base={"seed": 42},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )
        mock_cache_dir.return_value = Path(tempfile.mkdtemp())

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            trial_path = f.name

        try:
            run_local(trial_path, trial_path)
        finally:
            import os

            os.unlink(trial_path)

        assert mock_run_trial.call_count == 1


class TestRunLocalSweep:
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    @patch("jernerics.commands.execution.load_config")
    def test_sweep_runs_n_trials(self, mock_load, mock_cache_dir, mock_run_trial):
        import tempfile
        from pathlib import Path

        from jernerics.commands.execution import run_local

        mock_load.return_value = SweepConfig(
            base={"seed": 42},
            search_space=lambda trial: {"lr": trial.suggest_float("lr", 0.001, 0.1)},
            n_trials=5,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )
        mock_cache_dir.return_value = Path(tempfile.mkdtemp())

        with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as f:
            f.write(b"pass")
            trial_path = f.name

        try:
            run_local(trial_path, trial_path)
        finally:
            import os

            os.unlink(trial_path)

        assert mock_run_trial.call_count == 5
