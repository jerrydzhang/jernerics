from unittest.mock import patch

from jernerics.post_hook import PipelineResult, run_pipeline
from jernerics.retry import RetryContext


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
