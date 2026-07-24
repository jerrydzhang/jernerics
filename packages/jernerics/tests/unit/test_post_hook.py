from unittest.mock import patch

from jernerics.post_hook import PipelineResult, run_pipeline
from jernerics.retry import RetryContext


class TestRunPipeline:
    @patch("jernerics.post_hook.run_checker")
    def test_returns_retry_submitted_when_retries_happen(self, mock_run_checker):
        mock_run_checker.side_effect = None  # ran without exception = submitted retry

        result = run_pipeline(
            ctx_path="/cache/retry/ctx.json",
            chain_depth=0,
            tracking_dir="/cache/tracking/mystudy",
            storage_path="/cache/optuna/mystudy.journal",
        )

        assert result == PipelineResult.RETRY_SUBMITTED

    @patch("jernerics.post_hook.run_checker")
    def test_returns_sweep_complete_when_no_retries(self, mock_run_checker):
        # run_checker returns None when sweep is complete
        mock_run_checker.return_value = None

        result = run_pipeline(
            ctx_path="/cache/retry/ctx.json",
            chain_depth=0,
            tracking_dir="/cache/tracking/mystudy",
            storage_path="/cache/optuna/mystudy.journal",
        )

        assert result == PipelineResult.SWEEP_COMPLETE

    @patch("jernerics.post_hook.run_checker")
    def test_passes_args_to_run_checker(self, mock_run_checker):
        run_pipeline(
            ctx_path="/ctx.json",
            chain_depth=3,
            tracking_dir="/cache/tracking/s",
            storage_path="/cache/optuna/s.journal",
        )

        mock_run_checker.assert_called_once_with(ctx_path="/ctx.json", chain_depth=3)

    @patch("jernerics.post_hook.run_checker")
    def test_uploads_optuna_journal_on_sweep_complete(self, mock_run_checker, tmp_path):
        mock_run_checker.return_value = None  # sweep complete

        ctx = RetryContext(
            study_name="mystudy",
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            project_name="myproject",
        )
        ctx_path = tmp_path / "ctx.json"
        ctx_path.write_text(ctx.to_json())

        storage_path = tmp_path / "optuna" / "mystudy.journal"
        storage_path.parent.mkdir(parents=True)
        storage_path.write_text("journal-data")

        uploads = []

        def mock_upload(artifact_key, local_path):
            uploads.append((artifact_key, local_path))

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking"),
            storage_path=str(storage_path),
            upload_fn=mock_upload,
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        assert len(uploads) == 1
        assert uploads[0][0] == "myproject/mystudy/optuna.journal"
        assert uploads[0][1] == str(storage_path)

        assert result == PipelineResult.SWEEP_COMPLETE

    @patch("jernerics.post_hook.sync_artifacts")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_syncs_tracking_and_artifacts_on_sweep_complete(
        self, mock_run_checker, mock_replay, mock_sync_artifacts, tmp_path
    ):
        mock_run_checker.return_value = None
        base_url = "http://localhost:8000"

        ctx = RetryContext(
            study_name="mystudy",
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            project_name="myproject",
        )
        ctx_path = tmp_path / "ctx.json"
        ctx_path.write_text(ctx.to_json())

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking"),
            storage_path=str(tmp_path / "optuna" / "mystudy.journal"),
            base_url=base_url,
            upload_fn=lambda k, p: None,
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_called_once()
        mock_sync_artifacts.assert_called_once()
        assert mock_replay.mock_calls[0].kwargs["study"] == "mystudy"
        assert mock_sync_artifacts.mock_calls[0].kwargs["study"] == "mystudy"

        assert result == PipelineResult.SWEEP_COMPLETE

    @patch("jernerics.post_hook.sync_artifacts")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_skips_sync_when_no_stub(
        self, mock_run_checker, mock_replay, mock_sync_artifacts, tmp_path
    ):
        mock_run_checker.return_value = None

        ctx = RetryContext(
            study_name="mystudy",
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            project_name="myproject",
        )
        ctx_path = tmp_path / "ctx.json"
        ctx_path.write_text(ctx.to_json())

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking"),
            storage_path=str(tmp_path / "optuna" / "mystudy.journal"),
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_not_called()
        mock_sync_artifacts.assert_not_called()

    @patch("jernerics.post_hook.sync_artifacts")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_skips_optuna_and_sync_on_retry_submitted(
        self, mock_run_checker, mock_replay, mock_sync_artifacts, tmp_path
    ):
        mock_run_checker.side_effect = None  # submitted retries

        uploads = []

        result = run_pipeline(
            ctx_path="/cache/retry/ctx.json",
            chain_depth=0,
            tracking_dir="/cache/tracking/mystudy",
            storage_path="/cache/optuna/mystudy.journal",
            upload_fn=lambda k, p: uploads.append((k, p)),
            base_url="http://localhost:8000",
        )

        assert result == PipelineResult.RETRY_SUBMITTED
        assert len(uploads) == 0
        mock_replay.assert_not_called()
        mock_sync_artifacts.assert_not_called()

    @patch("jernerics.post_hook.run_checker")
    def test_skips_optuna_upload_when_no_upload_fn(self, mock_run_checker, tmp_path):
        mock_run_checker.return_value = None

        ctx = RetryContext(
            study_name="mystudy",
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            project_name="myproject",
        )
        ctx_path = tmp_path / "ctx.json"
        ctx_path.write_text(ctx.to_json())

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking"),
            storage_path=str(tmp_path / "optuna" / "mystudy.journal"),
        )

        assert result == PipelineResult.SWEEP_COMPLETE
