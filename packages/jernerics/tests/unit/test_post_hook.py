from unittest.mock import patch
from uuid import uuid4

import pytest
from jernerics.post_hook import PipelineResult, run_pipeline
from jernerics.retry import RetryContext
from jernerics.tracking.batch_sync import ReplayResult
from jernerics_schema import SweepSnapshotEvent, TrialSnapshotEvent, TrialState


def _write_ctx(tmp_path, study_name="mystudy"):
    ctx = RetryContext(
        study_name=study_name,
        backend_name="slurm",
        trial_relpath="trial.py",
        config_relpath="config.py",
        project_name="myproject",
    )
    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(ctx.to_json())
    return ctx_path


class TestRunPipeline:
    @patch("jernerics.post_hook.run_checker")
    def test_returns_retry_submitted_when_retries_happen(self, mock_run_checker):
        mock_run_checker.side_effect = None  # ran without exception = submitted retry

        result = run_pipeline(
            ctx_path="/cache/retry/ctx.json",
            chain_depth=2,
            tracking_dir="/cache/tracking/mystudy",
        )

        assert result == PipelineResult.RETRY_SUBMITTED

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_returns_sweep_complete_when_no_retries(
        self, mock_run_checker, mock_replay, tmp_path
    ):
        mock_run_checker.return_value = None  # sweep complete

        result = run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_not_called()

    @patch("jernerics.post_hook.run_checker")
    def test_passes_args_to_run_checker(self, mock_run_checker):
        run_pipeline(
            ctx_path="/ctx.json",
            chain_depth=3,
            tracking_dir="/cache/tracking/s",
        )

        mock_run_checker.assert_called_once_with(ctx_path="/ctx.json", chain_depth=3)

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_replays_tracking_on_sweep_complete_with_server(
        self, mock_run_checker, mock_replay, tmp_path
    ):
        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult()
        ctx_path = _write_ctx(tmp_path)

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
            base_url="http://localhost:8000",
            api_key="secret",
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_called_once()
        kwargs = mock_replay.mock_calls[0].kwargs
        assert kwargs["base_url"] == "http://localhost:8000"
        assert kwargs["api_key"] == "secret"
        assert kwargs["study"] == "mystudy"
        assert kwargs["tracking_dir"] == tmp_path / "tracking"

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_skips_replay_when_no_server(self, mock_run_checker, mock_replay, tmp_path):
        mock_run_checker.return_value = None

        result = run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_not_called()

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_skips_replay_on_retry_submitted(
        self, mock_run_checker, mock_replay, tmp_path
    ):
        mock_run_checker.side_effect = None  # submitted retries

        result = run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
            base_url="http://localhost:8000",
        )

        assert result == PipelineResult.RETRY_SUBMITTED
        mock_replay.assert_not_called()


class TestReconcileStudy:
    def _journal(self, tmp_path, name="s"):
        import optuna
        from optuna.storages.journal import JournalFileBackend, JournalStorage

        storage_url = str(tmp_path / f"{name}.journal")
        study = optuna.create_study(
            study_name=name,
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        return study, storage_url

    def _ctx(self, tmp_path, storage_url, study_name="s", project_name="proj"):
        return RetryContext(
            study_name=study_name,
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            storage_path=storage_url,
            project_name=project_name,
        )

    def test_missing_journal_returns_none(self, tmp_path):
        from jernerics.post_hook import reconcile_study

        ctx = self._ctx(tmp_path, str(tmp_path / "absent.journal"))

        assert reconcile_study(ctx, tmp_path / "tracking" / "s") is None

    def test_reconciles_every_frozen_trial_with_deterministic_ids(self, tmp_path):
        from jernerics.optuna_mirror import fallback_trial_id
        from jernerics.post_hook import reconcile_study
        from jernerics.tracking.jsonl_io import TrackingReader
        from jernerics_schema import sweep_id_for
        from optuna.trial import TrialState as OptunaState

        study, storage_url = self._journal(tmp_path)
        live = study.ask()
        live.suggest_float("lr", 0.1, 0.1)
        live.set_user_attr("jernerics_trial_id", str(_LIVE_ID))
        study.tell(live, 0.42)
        orphan = study.ask()
        orphan.suggest_float("lr", 0.2, 0.2)
        study.tell(orphan, state=OptunaState.FAIL)

        tracking_dir = tmp_path / "tracking" / "s"
        path = reconcile_study(self._ctx(tmp_path, storage_url), tracking_dir)

        assert path == tracking_dir / "submission" / "reconcile.jsonl"
        assert path is not None
        sweep_events = [
            event
            for event in TrackingReader(
                tracking_dir / "submission" / "reconcile-sweep.jsonl"
            )
            if isinstance(event, SweepSnapshotEvent)
        ]
        assert len(sweep_events) == 1
        assert sweep_events[0].sweep_id == sweep_id_for("proj", "s")
        events = [
            event
            for event in TrackingReader(path)
            if isinstance(event, TrialSnapshotEvent)
        ]
        sweep_id = sweep_id_for("proj", "s")
        assert len(events) == 2
        live_snapshot, orphan_snapshot = events
        assert live_snapshot.trial_id == _LIVE_ID
        assert live_snapshot.state == TrialState.COMPLETED
        assert live_snapshot.objective == pytest.approx(0.42)
        assert live_snapshot.params.root["lr"] == pytest.approx(0.1)
        assert live_snapshot.distributions is not None
        lr_distribution = live_snapshot.distributions.root["lr"]
        assert isinstance(lr_distribution, str)
        assert '"FloatDistribution"' in lr_distribution
        assert live_snapshot.attrs is not None
        assert live_snapshot.attrs.root["jernerics_trial_id"] == str(_LIVE_ID)
        assert orphan_snapshot.trial_id == fallback_trial_id(sweep_id, 1)
        assert orphan_snapshot.state == TrialState.FAILED
        assert orphan_snapshot.objective is None

    def test_repeated_reconciles_are_byte_identical(self, tmp_path):
        from jernerics.post_hook import reconcile_study
        from optuna.trial import TrialState

        study, storage_url = self._journal(tmp_path)
        trial = study.ask()
        trial.suggest_float("lr", 0.1, 0.1)
        study.tell(trial, 0.42)
        other = study.ask()
        study.tell(other, state=TrialState.FAIL)

        tracking_dir = tmp_path / "tracking" / "s"
        ctx = self._ctx(tmp_path, storage_url)
        first = reconcile_study(ctx, tracking_dir)
        assert first is not None
        first_bytes = first.read_bytes()

        second = reconcile_study(ctx, tracking_dir)

        assert second == first
        assert second is not None
        assert second.read_bytes() == first_bytes


_LIVE_ID = uuid4()
