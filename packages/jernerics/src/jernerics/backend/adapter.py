from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from jernerics.backend.models import JobInfo, SubmitResult


@dataclass
class SweepSubmissionParams:
    setup_command: str
    trial_command: str
    n_trials: int
    study_name: str
    log_dir: str
    cache_dir: str = ""
    post_hook_command: str | None = None
    max_parallel: int | None = None
    overrides: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SchedulerAdapter(Protocol):
    def submit_sweep(self, params: SweepSubmissionParams) -> SubmitResult: ...

    def render_sweep(self, params: SweepSubmissionParams) -> str: ...

    def submit_job(
        self, script: str, *, name: str, log_dir: str | None = None
    ) -> str: ...

    def list_jobs(self, include_completed: bool = False) -> list[JobInfo]: ...

    def cancel(self, job_id: str) -> bool: ...

    def cancel_all(self) -> bool: ...

    def get_status(self, job_id: str) -> str | None: ...

    def wait_for_completion(
        self, job_id: str, poll_interval: float = 30, timeout: float | None = None
    ) -> bool: ...

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        meta: dict | None = None,
    ) -> None: ...

    def cleanup(self) -> None: ...
