from unittest.mock import patch

from jernerics.post_hook import PipelineResult, run_pipeline


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
