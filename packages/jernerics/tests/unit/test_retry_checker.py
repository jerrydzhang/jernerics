"""Tests for the retry_checker using assemble_infrastructure + submit_sweep."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from jernerics.retry import RetryContext
from optuna.trial import FrozenTrial, TrialState


def _make_trial(number: int, state: int, params: dict | None = None) -> FrozenTrial:
    trial = MagicMock(spec=FrozenTrial)
    trial.number = number
    trial.state = state
    trial.params = params if params is not None else {"lr": 0.01}
    return trial


def _make_trials_list(*trials):
    """Build a list indexed by trial number (as optuna does)."""
    if not trials:
        return []
    max_num = max(t.number for t in trials)
    result = [_make_trial(i, TrialState.FAIL) for i in range(max_num + 1)]
    for t in trials:
        result[t.number] = t
    return result


def _write_ctx(tmp_path: Path, ctx: RetryContext) -> str:
    ctx_file = tmp_path / "ctx.json"
    ctx_file.write_text(ctx.to_json())
    return str(ctx_file)


def _write_heartbeat(tracking_dir: Path, trial_number: int, age: float, now: float):
    hb_dir = tracking_dir / "heartbeats"
    hb_dir.mkdir(parents=True, exist_ok=True)
    hb_path = hb_dir / f"{trial_number}.heartbeat"
    hb_path.touch()
    mtime = now - age
    os.utime(hb_path, (mtime, mtime))


def _setup_common_mocks(
    mock_load_backend,
    mock_load_config,
    mock_assemble,
    mock_submit,
    tmp_path,
    *,
    cli_overrides=None,
    backend_overrides=None,
):
    """Shared test setup for retry checker tests."""
    study = MagicMock()
    study.trials = _make_trials_list(
        _make_trial(0, TrialState.COMPLETE),
        _make_trial(5, TrialState.RUNNING, {"lr": 0.01}),
    )

    tracking_dir = tmp_path / "tracking" / "mystudy"
    _write_heartbeat(tracking_dir, 5, age=200, now=1000.0)

    storage_path = str(tmp_path / "optuna" / "mystudy.journal")
    Path(storage_path).parent.mkdir(parents=True, exist_ok=True)
    Path(storage_path).touch()

    ctx = RetryContext(
        study_name="mystudy",
        backend_name="hpc",
        dag_relpath="dag.py",
        config_relpath="config.py",
        cli_overrides=cli_overrides or {},
        storage_path=storage_path,
        tracking_dir=str(tracking_dir),
        project_dir=str(tmp_path),
        project_name="proj",
        host_home="/home/user",
    )
    ctx_path = _write_ctx(tmp_path, ctx)

    from jernerics.config import SharedConfig, SlurmConfig

    shared = SharedConfig(
        name="hpc",
        type="slurm",
        remote_dir="/scratch/proj",
        container_type="apptainer",
        grace_period_s=0,
        stale_after_s=120,
        max_retries=3,
        chain_depth_cap=20,
    )
    backend_config = MagicMock()
    backend_config.shared = shared
    backend_config.backend = SlurmConfig()
    backend_config.container = MagicMock()
    mock_load_backend.return_value = backend_config

    sweep = MagicMock()
    sweep.n_trials = 6
    sweep.direction = "minimize"
    sweep.backend_overrides = backend_overrides or {}
    sweep.sampler = None
    mock_load_config.return_value = sweep

    adapter = MagicMock()
    from jernerics.backend.models import JobSubmission, SubmitResult

    adapter.submit_sweep.return_value = SubmitResult(
        submissions=[JobSubmission(job_id="123", n_trials=4)]
    )

    from jernerics.backend.container import NoContainer
    from jernerics.backend.path_resolver import PathResolver
    from jernerics.backend.submission import SweepInfrastructure

    infra = SweepInfrastructure(
        adapter=adapter,
        container=NoContainer(),
        paths=PathResolver(
            remote_dir="/scratch/proj",
            cache_dir="/scratch/cache",
            container=NoContainer(),
            project_name="proj",
        ),
    )
    mock_assemble.return_value = infra

    from jernerics.backend.models import SubmitResult as SR

    mock_submit.return_value = SR(submissions=[JobSubmission(job_id="123", n_trials=4)])

    return ctx_path, study


class TestRetryCheckerArtifactEnv:
    @patch("jernerics.retry_checker.submit_sweep")
    @patch("jernerics.retry_checker.assemble_infrastructure")
    @patch("jernerics.retry_checker.load_config")
    @patch("jernerics.retry_checker.load_backend_config")
    def test_forwards_artifact_env_to_build_sweep_commands(
        self,
        mock_load_backend,
        mock_load_config,
        mock_assemble,
        mock_submit,
        tmp_path,
    ):
        from jernerics.retry_checker import run_checker

        ctx_path, study = _setup_common_mocks(
            mock_load_backend,
            mock_load_config,
            mock_assemble,
            mock_submit,
            tmp_path,
        )

        env_vars = {
            "AWS_ENDPOINT_URL": "http://minio:9000",
            "JERNERICS_ARTIFACT_BUCKET": "jernerics",
        }

        with (
            patch("jernerics.retry_checker.optuna") as mock_optuna,
            patch("jernerics.retry_checker.time") as mock_time,
            patch("jernerics.retry_checker.read_ledger", return_value={}),
            patch("jernerics.retry_checker.write_ledger"),
            patch("jernerics.retry_checker.load_tracking_server", return_value=None),
            patch.dict(os.environ, env_vars, clear=False),
        ):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            mock_optuna.load_study.return_value = study
            mock_optuna.trial.TrialState = TrialState
            mock_optuna.storages.journal.JournalFileBackend.return_value = MagicMock()
            mock_optuna.storages.journal.JournalStorage.return_value = MagicMock()

            run_checker(ctx_path=ctx_path, chain_depth=0)

        # submit_sweep was called — artifact env is resolved internally
        mock_submit.assert_called_once()


class TestRetryCheckerUsesSubmitSweep:
    """Verify retry_checker calls assemble_infrastructure + submit_sweep."""

    @patch("jernerics.retry_checker.submit_sweep")
    @patch("jernerics.retry_checker.assemble_infrastructure")
    @patch("jernerics.retry_checker.load_config")
    @patch("jernerics.retry_checker.load_backend_config")
    def test_calls_assemble_infrastructure(
        self,
        mock_load_backend,
        mock_load_config,
        mock_assemble,
        mock_submit,
        tmp_path,
    ):
        from jernerics.retry_checker import run_checker

        ctx_path, study = _setup_common_mocks(
            mock_load_backend,
            mock_load_config,
            mock_assemble,
            mock_submit,
            tmp_path,
        )

        with (
            patch("jernerics.retry_checker.optuna") as mock_optuna,
            patch("jernerics.retry_checker.time") as mock_time,
            patch("jernerics.retry_checker.read_ledger", return_value={}),
            patch("jernerics.retry_checker.write_ledger"),
            patch("jernerics.retry_checker.load_tracking_server", return_value=None),
        ):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            mock_optuna.load_study.return_value = study
            mock_optuna.trial.TrialState = TrialState
            mock_optuna.storages.journal.JournalFileBackend.return_value = MagicMock()
            mock_optuna.storages.journal.JournalStorage.return_value = MagicMock()

            run_checker(ctx_path=ctx_path, chain_depth=0)

        mock_assemble.assert_called_once()
        mock_submit.assert_called_once()

    @patch("jernerics.retry_checker.submit_sweep")
    @patch("jernerics.retry_checker.assemble_infrastructure")
    @patch("jernerics.retry_checker.load_config")
    @patch("jernerics.retry_checker.load_backend_config")
    def test_no_submit_when_complete(
        self,
        mock_load_backend,
        mock_load_config,
        mock_assemble,
        mock_submit,
        tmp_path,
    ):
        from jernerics.retry_checker import run_checker

        study = MagicMock()
        study.trials = _make_trials_list(
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.COMPLETE),
            _make_trial(2, TrialState.COMPLETE),
        )

        tracking_dir = tmp_path / "tracking" / "mystudy"
        tracking_dir.mkdir(parents=True)

        storage_path = str(tmp_path / "optuna" / "mystudy.journal")
        Path(storage_path).parent.mkdir(parents=True, exist_ok=True)
        Path(storage_path).touch()

        ctx = RetryContext(
            study_name="mystudy",
            backend_name="hpc",
            dag_relpath="dag.py",
            config_relpath="config.py",
            storage_path=storage_path,
            tracking_dir=str(tracking_dir),
            project_dir=str(tmp_path),
            project_name="proj",
            host_home="/home/user",
        )
        ctx_path = _write_ctx(tmp_path, ctx)

        from jernerics.config import SharedConfig

        shared = SharedConfig(
            name="hpc",
            type="slurm",
            remote_dir="/scratch/proj",
            container_type="apptainer",
            grace_period_s=0,
            stale_after_s=120,
            max_retries=3,
            chain_depth_cap=20,
        )
        backend_config = MagicMock()
        backend_config.shared = shared
        backend_config.backend = MagicMock()
        backend_config.container = MagicMock()
        mock_load_backend.return_value = backend_config

        sweep = MagicMock()
        sweep.n_trials = 3
        sweep.backend_overrides = {}
        mock_load_config.return_value = sweep

        with (
            patch("jernerics.retry_checker.optuna") as mock_optuna,
            patch("jernerics.retry_checker.time") as mock_time,
            patch("jernerics.retry_checker.read_ledger", return_value={}),
        ):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            mock_optuna.load_study.return_value = study
            mock_optuna.trial.TrialState = TrialState
            mock_optuna.storages.journal.JournalFileBackend.return_value = MagicMock()
            mock_optuna.storages.journal.JournalStorage.return_value = MagicMock()

            run_checker(ctx_path=ctx_path, chain_depth=0)

        mock_assemble.assert_not_called()
        mock_submit.assert_not_called()

    @patch("jernerics.retry_checker.submit_sweep")
    @patch("jernerics.retry_checker.assemble_infrastructure")
    @patch("jernerics.retry_checker.load_config")
    @patch("jernerics.retry_checker.load_backend_config")
    def test_passes_overrides_to_submit_sweep(
        self,
        mock_load_backend,
        mock_load_config,
        mock_assemble,
        mock_submit,
        tmp_path,
    ):
        from jernerics.retry_checker import run_checker

        ctx_path, study = _setup_common_mocks(
            mock_load_backend,
            mock_load_config,
            mock_assemble,
            mock_submit,
            tmp_path,
            cli_overrides={"partition": "gpu", "mem": "64G"},
            backend_overrides={"hpc": {"time": "4:00:00"}},
        )

        with (
            patch("jernerics.retry_checker.optuna") as mock_optuna,
            patch("jernerics.retry_checker.time") as mock_time,
            patch("jernerics.retry_checker.read_ledger", return_value={}),
            patch("jernerics.retry_checker.write_ledger"),
            patch("jernerics.retry_checker.load_tracking_server", return_value=None),
        ):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            mock_optuna.load_study.return_value = study
            mock_optuna.trial.TrialState = TrialState
            mock_optuna.storages.journal.JournalFileBackend.return_value = MagicMock()
            mock_optuna.storages.journal.JournalStorage.return_value = MagicMock()

            run_checker(ctx_path=ctx_path, chain_depth=0)

        # Verify overrides were passed to submit_sweep
        call_kwargs = mock_submit.call_args
        assert call_kwargs[1]["experiment_overrides"]["time"] == "4:00:00"
        assert call_kwargs[1]["cli_overrides"]["partition"] == "gpu"
        assert call_kwargs[1]["cli_overrides"]["mem"] == "64G"

    @patch("jernerics.retry_checker.submit_sweep")
    @patch("jernerics.retry_checker.assemble_infrastructure")
    @patch("jernerics.retry_checker.load_config")
    @patch("jernerics.retry_checker.load_backend_config")
    def test_passes_chain_depth_plus_one(
        self,
        mock_load_backend,
        mock_load_config,
        mock_assemble,
        mock_submit,
        tmp_path,
    ):
        from jernerics.retry_checker import run_checker

        ctx_path, study = _setup_common_mocks(
            mock_load_backend,
            mock_load_config,
            mock_assemble,
            mock_submit,
            tmp_path,
        )

        with (
            patch("jernerics.retry_checker.optuna") as mock_optuna,
            patch("jernerics.retry_checker.time") as mock_time,
            patch("jernerics.retry_checker.read_ledger", return_value={}),
            patch("jernerics.retry_checker.write_ledger"),
            patch("jernerics.retry_checker.load_tracking_server", return_value=None),
        ):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            mock_optuna.load_study.return_value = study
            mock_optuna.trial.TrialState = TrialState
            mock_optuna.storages.journal.JournalFileBackend.return_value = MagicMock()
            mock_optuna.storages.journal.JournalStorage.return_value = MagicMock()

            run_checker(ctx_path=ctx_path, chain_depth=2)

        call_kwargs = mock_submit.call_args
        assert call_kwargs[1]["chain_depth"] == 3

    @patch("jernerics.retry_checker.submit_sweep")
    @patch("jernerics.retry_checker.assemble_infrastructure")
    @patch("jernerics.retry_checker.load_config")
    @patch("jernerics.retry_checker.load_backend_config")
    def test_loads_tracking_server(
        self,
        mock_load_backend,
        mock_load_config,
        mock_assemble,
        mock_submit,
        tmp_path,
    ):
        from jernerics.retry_checker import run_checker

        ctx_path, study = _setup_common_mocks(
            mock_load_backend,
            mock_load_config,
            mock_assemble,
            mock_submit,
            tmp_path,
        )

        with (
            patch("jernerics.retry_checker.optuna") as mock_optuna,
            patch("jernerics.retry_checker.time") as mock_time,
            patch("jernerics.retry_checker.read_ledger", return_value={}),
            patch("jernerics.retry_checker.write_ledger"),
            patch("jernerics.retry_checker.load_tracking_server") as mock_ts,
        ):
            mock_time.time.return_value = 1000.0
            mock_time.sleep = MagicMock()
            mock_optuna.load_study.return_value = study
            mock_optuna.trial.TrialState = TrialState
            mock_optuna.storages.journal.JournalFileBackend.return_value = MagicMock()
            mock_optuna.storages.journal.JournalStorage.return_value = MagicMock()
            mock_ts.return_value = "https://track.example.com"

            run_checker(ctx_path=ctx_path, chain_depth=0)

        call_kwargs = mock_submit.call_args
        assert call_kwargs[1]["tracking_server"] == "https://track.example.com"
