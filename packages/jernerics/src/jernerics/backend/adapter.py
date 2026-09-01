from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from jernerics.backend.models import JobInfo, SubmitResult


@dataclass
class SweepSubmissionParams:
    """``overrides`` is a flat dict interpreted by the adapter rather than a
    typed config object -- it is already user-facing as a flat dict (``--set
    partition=priority``), so a typed translation layer would just be dict ->
    dataclass -> dict with the adapter as the natural interpreter.
    """

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
    """Per-scheduler submission and job lifecycle.

    Adapters receive command strings that are already container-wrapped with
    resolved paths (see SweepSubmissionParams). They never see the
    ContainerRuntime or PathResolver, which keeps their job narrow: "given these
    runnable strings, schedule them on my scheduler." Letting the adapter do the
    wrapping would duplicate composition logic across every scheduler and pull
    PathResolver/ContainerRuntime knowledge into a component that should not
    need it.
    """

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
        array_index: int | None = None,
        meta: dict | None = None,
    ) -> None: ...

    def cleanup(self) -> None: ...
