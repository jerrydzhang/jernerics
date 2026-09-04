"""Tests for SchedulerAdapter protocol and SweepSubmissionParams."""

from jernerics.backend.adapter import SchedulerAdapter, SweepSubmissionParams
from jernerics.backend.models import JobSubmission, SubmitResult


class TestSweepSubmissionParams:
    def test_construct_with_all_fields(self):
        params = SweepSubmissionParams(
            setup_command="setup_cmd",
            trial_command="trial_cmd",
            post_hook_command="checker_cmd",
            n_trials=10,
            max_parallel=4,
            study_name="my_study",
            log_dir="/cache/logs",
            overrides={"partition": "priority"},
        )
        assert params.setup_command == "setup_cmd"
        assert params.trial_command == "trial_cmd"
        assert params.post_hook_command == "checker_cmd"
        assert params.n_trials == 10
        assert params.max_parallel == 4
        assert params.study_name == "my_study"
        assert params.log_dir == "/cache/logs"
        assert params.overrides == {"partition": "priority"}

    def test_post_hook_command_defaults_none(self):
        params = SweepSubmissionParams(
            setup_command="s",
            trial_command="t",
            n_trials=1,
            study_name="s",
            log_dir="/logs",
        )
        assert params.post_hook_command is None

    def test_max_parallel_defaults_none(self):
        params = SweepSubmissionParams(
            setup_command="s",
            trial_command="t",
            n_trials=1,
            study_name="s",
            log_dir="/logs",
        )
        assert params.max_parallel is None

    def test_overrides_defaults_empty(self):
        params = SweepSubmissionParams(
            setup_command="s",
            trial_command="t",
            n_trials=1,
            study_name="s",
            log_dir="/logs",
        )
        assert params.overrides == {}


class TestJobSubmission:
    def test_construct_with_job_id_only(self):
        sub = JobSubmission(job_id="12345")
        assert sub.job_id == "12345"
        assert sub.output_pattern is None
        assert sub.error_pattern is None
        assert sub.n_trials == 0

    def test_construct_with_all_fields(self):
        sub = JobSubmission(
            job_id="100",
            output_pattern="/logs/%A_%a.out",
            error_pattern="/logs/%A_%a.err",
            n_trials=50,
        )
        assert sub.output_pattern == "/logs/%A_%a.out"
        assert sub.error_pattern == "/logs/%A_%a.err"
        assert sub.n_trials == 50


class TestSubmitResult:
    def test_submissions_list(self):
        result = SubmitResult(
            submissions=[
                JobSubmission(job_id="100", n_trials=10),
                JobSubmission(job_id="101", n_trials=0),
            ]
        )
        assert len(result.submissions) == 2
        assert result.submissions[0].job_id == "100"
        assert result.submissions[1].job_id == "101"


class TestSchedulerAdapterProtocol:
    def test_minimal_implementation_satisfies_protocol(self):
        class FakeAdapter:
            def submit_sweep(self, params): ...

            def render_sweep(self, params): ...

            def submit_job(self, script, *, name, log_dir=None): ...

            def list_jobs(self, include_completed=False): ...

            def cancel(self, job_id): ...

            def cancel_all(self): ...

            def get_status(self, job_id): ...

            def fetch_job_resources(self, job_id): ...

            def wait_for_completion(self, job_id, poll_interval=30, timeout=None): ...

            def get_logs(
                self, job_id, *, follow=False, stderr=False, array_index=None, meta=None
            ): ...

            def cleanup(self): ...

            def valid_override_keys(self): ...

        assert isinstance(FakeAdapter(), SchedulerAdapter)
