from unittest.mock import MagicMock, patch

import pytest
from jernerics.config import SweepConfig


class TestLocalBackendPostHook:
    @patch("jernerics.backend.local_backend.ship_events_file")
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
        mock_ship,
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

    @patch("jernerics.backend.local_backend.sweep_manifest_blobs")
    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.ship_events_file")
    @patch("jernerics.backend.local_backend.resolve_tracking_ship")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_failed_sweep_emits_failed_state_before_raising(
        self,
        mock_cache_dir,
        mock_run_trial,
        mock_resolve_tracking_ship,
        mock_ship,
        mock_replay,
        mock_blobs,
        tmp_path,
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path
        mock_resolve_tracking_ship.return_value = ("http://localhost:8000", None)
        mock_run_trial.side_effect = SystemExit(1)
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
        )

        with pytest.raises(RuntimeError, match="One or more trials failed"):
            LocalBackend(tracking_server="http://localhost:8000").submit_sweep(spec)

        path = (
            tmp_path
            / "tracking"
            / "mystudy"
            / "submission"
            / f"{spec.submission_id}.jsonl"
        )
        events = _read_submission_events(path)
        assert events[-1].tag == "sweep_snapshot"
        assert events[-1].state == "failed"
        assert mock_ship.call_count == 2
        mock_replay.assert_called_once()

    @patch("jernerics.backend.local_backend.sweep_manifest_blobs")
    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.ship_events_file")
    @patch("jernerics.backend.local_backend.resolve_tracking_ship")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_blob_retry_runs_before_replay(
        self,
        mock_cache_dir,
        mock_run_trial,
        mock_resolve_tracking_ship,
        mock_ship,
        mock_replay,
        mock_blobs,
        tmp_path,
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path
        mock_resolve_tracking_ship.return_value = ("http://localhost:8000", None)
        order = []
        mock_blobs.side_effect = lambda tracking_dir, base_url, api_key: order.append(
            "blobs"
        )
        mock_replay.side_effect = lambda **kwargs: order.append("replay")
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
        )

        LocalBackend(tracking_server="http://localhost:8000").submit_sweep(spec)

        assert order == ["blobs", "replay"]
        mock_blobs.assert_called_once_with(
            tmp_path / "tracking" / "mystudy", "http://localhost:8000", None
        )


class TestLocalBackendSubmissionEvents:
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_emits_submission_events_when_project_named(
        self, mock_cache_dir, mock_run_trial, tmp_path
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission
        from jernerics_schema import sweep_id_for

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
            "sweep_snapshot",
        ]
        submission = events[1]
        assert submission.expected_trials == 1
        assert submission.git_hash == "abc123"
        assert submission.config_source == str(tmp_path / "config.py")
        assert events[2].role == "trials"
        assert events[2].scheduler_job_id == "local"
        terminal = events[3]
        assert terminal.state == "completed"
        assert terminal.name == "mystudy"
        assert terminal.sweep_id == sweep_id_for("proj", "mystudy")

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

    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.ship_events_file")
    @patch("jernerics.backend.local_backend.resolve_tracking_ship")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_ships_submission_events_before_first_trial(
        self,
        mock_cache_dir,
        mock_run_trial,
        mock_resolve_tracking_ship,
        mock_ship,
        mock_replay,
        tmp_path,
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path
        mock_resolve_tracking_ship.return_value = ("http://localhost:8000", None)
        calls = []
        mock_ship.side_effect = lambda path, base_url, api_key=None: calls.append(
            ("ship", path)
        )
        mock_run_trial.side_effect = lambda **kwargs: calls.append(("trial", None))
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
        )

        LocalBackend(tracking_server="http://localhost:8000").submit_sweep(spec)

        submission_path = (
            tmp_path
            / "tracking"
            / "mystudy"
            / "submission"
            / f"{spec.submission_id}.jsonl"
        )
        assert calls == [
            ("ship", submission_path),
            ("trial", None),
            ("ship", submission_path),
        ]

    @patch("jernerics.backend.local_backend.replay_tracking")
    @patch("jernerics.backend.local_backend.ship_events_file")
    @patch("jernerics.backend.local_backend.resolve_tracking_ship")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    def test_shipping_failure_does_not_fail_submission(
        self,
        mock_cache_dir,
        mock_run_trial,
        mock_resolve_tracking_ship,
        mock_ship,
        mock_replay,
        tmp_path,
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission

        mock_cache_dir.return_value = tmp_path
        mock_resolve_tracking_ship.return_value = ("http://localhost:8000", None)
        mock_ship.return_value = False
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
        )

        result = LocalBackend(tracking_server="http://localhost:8000").submit_sweep(
            spec
        )

        assert result.submissions[0].job_id == "local"
        mock_run_trial.assert_called_once()


class TestSchemeLessTrackingServer:
    @patch("jernerics.backend.local_backend.ship_events_file")
    def test_ship_submission_events_fails_fast_without_shipping(
        self, mock_ship, tmp_path
    ):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission
        from jernerics.tracking.infra import TrackingServerSchemeError

        spec = SweepSubmission(
            trial_path=tmp_path / "trial.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=str(tmp_path / "optuna" / "mystudy.journal"),
            n_trials=1,
        )

        with pytest.raises(TrackingServerSchemeError) as excinfo:
            LocalBackend(
                tracking_server="atlas.taile454b.ts.net:443"
            )._ship_submission_events(spec, tmp_path / "submission" / "s.jsonl")

        mock_ship.assert_not_called()
        message = str(excinfo.value)
        assert "JERNERICS_TRACKING_SERVER" in message
        assert "[tool.jernerics] tracking_server" in message

    @patch("jernerics.backend.local_backend.ship_events_file")
    def test_spec_server_addr_is_validated_too(self, mock_ship, tmp_path):
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission
        from jernerics.tracking.infra import TrackingServerSchemeError

        spec = SweepSubmission(
            trial_path=tmp_path / "trial.py",
            config_path=tmp_path / "config.py",
            study_name="mystudy",
            storage_url=str(tmp_path / "optuna" / "mystudy.journal"),
            n_trials=1,
            server_addr="atlas.example:443",
        )

        with pytest.raises(TrackingServerSchemeError, match=r"atlas\.example:443"):
            LocalBackend()._ship_submission_events(
                spec, tmp_path / "submission" / "s.jsonl"
            )

        mock_ship.assert_not_called()


def _read_submission_events(path):
    from jernerics.tracking.jsonl_io import TrackingReader

    with TrackingReader(path) as reader:
        return list(reader)


class TestRunLocalSingleConfig:
    @patch("jernerics.backend.local_backend.ship_events_file")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    @patch("jernerics.commands.execution.load_config")
    def test_single_config_runs_once(
        self, mock_load, mock_cache_dir, mock_run_trial, mock_ship, monkeypatch
    ):
        monkeypatch.delenv("JERNERICS_TRACKING_SERVER", raising=False)
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
    @patch("jernerics.backend.local_backend.ship_events_file")
    @patch("jernerics.backend.local_backend.run_trial")
    @patch("jernerics.backend.local_backend.cache_dir")
    @patch("jernerics.commands.execution.load_config")
    def test_sweep_runs_n_trials(
        self, mock_load, mock_cache_dir, mock_run_trial, mock_ship, monkeypatch
    ):
        monkeypatch.delenv("JERNERICS_TRACKING_SERVER", raising=False)
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


class TestRunRemoteCommand:
    @patch("jernerics.commands.execution._get_backend")
    @patch("jernerics.commands.execution.load_config")
    @patch("jernerics.commands.execution.find_pyproject_dir")
    def test_slurm_submit_error_exits_four(
        self, mock_find, mock_load, mock_get_backend, tmp_path, capsys
    ):
        from jernerics.backend.slurm.adapter import SlurmSubmitError
        from jernerics.commands.execution import run_remote
        from jernerics.config import ExitCode

        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text("pass")

        mock_find.return_value = tmp_path
        mock_load.return_value = SweepConfig(
            base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )
        backend = MagicMock()
        backend.prepare_and_submit.side_effect = SlurmSubmitError(
            "checker submission failed; array job 10001 already queued"
        )
        mock_get_backend.return_value = (backend, "proj", tmp_path)

        with pytest.raises(SystemExit) as exc_info:
            run_remote(
                str(tmp_path / "trial.py"),
                str(tmp_path / "config.py"),
                backend_name="hpc",
            )

        assert exc_info.value.code == ExitCode.SLURM_ERROR
        assert "array job 10001 already queued" in capsys.readouterr().out

    @patch("jernerics.commands.execution.load_config")
    @patch("jernerics.commands.execution.find_pyproject_dir")
    def test_set_unknown_key_exits_config_error(
        self, mock_find, mock_load, tmp_path, capsys
    ):
        from jernerics.commands.execution import run_remote
        from jernerics.config import ExitCode

        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text("pass")
        mock_find.return_value = tmp_path
        mock_load.return_value = SweepConfig(
            base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )

        with pytest.raises(SystemExit) as exc_info:
            run_remote(
                str(tmp_path / "trial.py"),
                str(tmp_path / "config.py"),
                backend_name="hpc",
                set_opt=["bogus=1"],
            )

        assert exc_info.value.code == ExitCode.CONFIG_ERROR
        out = capsys.readouterr().out
        assert "bogus" in out
        assert "partition" in out
        assert "cpus-per-task" in out

    @patch("jernerics.commands.execution.load_config")
    @patch("jernerics.commands.execution.find_pyproject_dir")
    def test_set_param_missing_equals_exits_config_error(
        self, mock_find, mock_load, tmp_path, capsys
    ):
        from jernerics.commands.execution import run_remote
        from jernerics.config import ExitCode

        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text("pass")
        mock_find.return_value = tmp_path
        mock_load.return_value = SweepConfig(
            base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )

        with pytest.raises(SystemExit) as exc_info:
            run_remote(
                str(tmp_path / "trial.py"),
                str(tmp_path / "config.py"),
                backend_name="hpc",
                set_param_opt=["target"],
            )

        assert exc_info.value.code == ExitCode.CONFIG_ERROR
        assert "--set-param" in capsys.readouterr().out

    @patch("jernerics.commands.execution.load_config")
    @patch("jernerics.commands.execution.find_pyproject_dir")
    def test_set_param_empty_key_exits_config_error(
        self, mock_find, mock_load, tmp_path, capsys
    ):
        from jernerics.commands.execution import run_remote
        from jernerics.config import ExitCode

        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text("pass")
        mock_find.return_value = tmp_path
        mock_load.return_value = SweepConfig(
            base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )

        with pytest.raises(SystemExit) as exc_info:
            run_remote(
                str(tmp_path / "trial.py"),
                str(tmp_path / "config.py"),
                backend_name="hpc",
                set_param_opt=["=3200"],
            )

        assert exc_info.value.code == ExitCode.CONFIG_ERROR

    @patch("jernerics.commands.execution._get_backend")
    @patch("jernerics.commands.execution.load_config")
    @patch("jernerics.commands.execution.find_pyproject_dir")
    def test_set_param_coerces_values_into_spec(
        self, mock_find, mock_load, mock_get_backend, tmp_path
    ):
        from jernerics.commands.execution import run_remote

        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text("pass")
        mock_find.return_value = tmp_path
        mock_load.return_value = SweepConfig(
            base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )
        backend = MagicMock()
        backend.prepare_and_submit.return_value = None
        mock_get_backend.return_value = (backend, "proj", tmp_path)

        run_remote(
            str(tmp_path / "trial.py"),
            str(tmp_path / "config.py"),
            backend_name="hpc",
            set_param_opt=["target=3200", "name=foo", "flag=true"],
        )

        spec = backend.prepare_and_submit.call_args[0][0]
        assert spec.param_overrides == {"target": 3200, "name": "foo", "flag": True}
        assert isinstance(spec.param_overrides["target"], int)

    @patch("jernerics.commands.execution._get_backend")
    @patch("jernerics.commands.execution.load_config")
    @patch("jernerics.commands.execution.find_pyproject_dir")
    def test_set_valid_sbatch_key_passes_cli_overrides(
        self, mock_find, mock_load, mock_get_backend, tmp_path
    ):
        from jernerics.commands.execution import run_remote

        (tmp_path / "trial.py").write_text("pass")
        (tmp_path / "config.py").write_text("pass")
        mock_find.return_value = tmp_path
        mock_load.return_value = SweepConfig(
            base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            backend_overrides={},
            objective=None,
        )
        backend = MagicMock()
        backend.prepare_and_submit.return_value = None
        mock_get_backend.return_value = (backend, "proj", tmp_path)

        run_remote(
            str(tmp_path / "trial.py"),
            str(tmp_path / "config.py"),
            backend_name="hpc",
            set_opt=["partition=debug"],
        )

        assert backend.prepare_and_submit.call_args[1]["cli_overrides"] == {
            "partition": "debug"
        }


class TestRunLocalGridSweep:
    def test_grid_sweep_runs_every_combination_once(self, tmp_path):
        import json

        import optuna
        from jernerics.backend.local_backend import LocalBackend
        from jernerics.backend.models import SweepSubmission
        from jernerics.config import load_config
        from optuna.storages.journal import JournalFileBackend, JournalStorage

        trial_file = tmp_path / "trial.py"
        trial_file.write_text(
            "from jernerics import trial_config, trial_tracker\n"
            "config = trial_config()\n"
            "tracker = trial_tracker()\n"
            "tracker.finish({'loss': config['lr']})\n"
        )
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {'seed': 7}\n"
            "grid = {'lr': [0.1, 0.2, 0.3], 'mode': ['a', 'b']}\n"
            "def objective(results):\n    return results['loss']\n"
        )
        sweep = load_config(str(config_file))
        assert sweep.n_trials == 6

        spec = SweepSubmission(
            trial_path=trial_file,
            config_path=config_file,
            study_name="gridtest",
            storage_url=str(tmp_path / "gridtest.journal"),
            n_trials=sweep.n_trials,
            tracking_dir=tmp_path / "tracking" / "gridtest",
            grid=sweep.grid,
        )

        LocalBackend().submit_sweep(spec, direction="minimize")

        study = optuna.load_study(
            study_name="gridtest",
            storage=JournalStorage(
                JournalFileBackend(str(tmp_path / "gridtest.journal"))
            ),
        )
        assert len(study.trials) == 6
        assert all(t.state == optuna.trial.TrialState.COMPLETE for t in study.trials)
        expected = {(lr, mode) for lr in (0.1, 0.2, 0.3) for mode in ("a", "b")}
        assert {(t.params["lr"], t.params["mode"]) for t in study.trials} == expected

        resolved_configs = [
            json.loads((tmp_path / "configs" / f"trial_{i}.json").read_text())
            for i in range(6)
        ]
        assert {(c["lr"], c["mode"]) for c in resolved_configs} == expected
        assert all(c["seed"] == 7 for c in resolved_configs)


class TestRunLocalGridConfigErrors:
    def test_grid_with_search_space_exits_config_error(self, tmp_path):
        from jernerics.commands.execution import run_local
        from jernerics.config import ExitCode

        trial_file = tmp_path / "trial.py"
        trial_file.write_text("pass\n")
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "grid = {'lr': [0.1, 0.2]}\n"
            "def search_space(trial):\n"
            "    return {'wd': trial.suggest_float('wd', 0.0, 0.5)}\n"
        )

        with pytest.raises(SystemExit) as exc_info:
            run_local(str(trial_file), str(config_file))

        assert exc_info.value.code == ExitCode.CONFIG_ERROR

    def test_grid_n_trials_mismatch_exits_config_error(self, tmp_path, capsys):
        from jernerics.commands.execution import run_local
        from jernerics.config import ExitCode

        trial_file = tmp_path / "trial.py"
        trial_file.write_text("pass\n")
        config_file = tmp_path / "config.py"
        config_file.write_text("base = {}\ngrid = {'lr': [0.1, 0.2]}\nn_trials = 5\n")

        with pytest.raises(SystemExit) as exc_info:
            run_local(str(trial_file), str(config_file))

        assert exc_info.value.code == ExitCode.CONFIG_ERROR
        output = capsys.readouterr().out
        assert "n_trials=5" in output
        assert "grid size 2" in output
