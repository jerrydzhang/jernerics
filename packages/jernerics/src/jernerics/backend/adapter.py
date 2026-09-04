import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from jernerics_schema import JERNERICS_NAMESPACE, JobResourceEvent

from jernerics.backend.models import JobInfo, SubmitResult


@dataclass(frozen=True)
class JobResourceSnapshot:
    """Resource facts for one scheduler job; None means unavailable."""

    job_id: str
    state: str | None
    exit_code: str | None
    wall_time_s: float | None
    cpu_time_s: float | None
    cpu_pct: float | None
    max_rss_mb: float | None
    ave_rss_mb: float | None
    alloc_cpus: int | None
    req_mem: str | None
    alloc_tres: str | None
    node_list: str | None


@dataclass(frozen=True)
class JobResourcesResult:
    """One accounting query: snapshots per concrete job, or an error string."""

    snapshots: list[JobResourceSnapshot] = field(default_factory=list)
    error: str | None = None


def build_job_resource_event(
    snapshot: JobResourceSnapshot,
    *,
    study_name: str | None = None,
    submission_id: str | None = None,
    recorded_at: datetime | None = None,
) -> JobResourceEvent:
    """Snapshot as a tracking event with a per-job deterministic event id.

    The uuid5 id keyed on the scheduler job id makes re-captures (post-hook
    rerun, backfill CLI) ingest as duplicates instead of duplicate rows.
    """
    return JobResourceEvent(
        event_id=uuid.uuid5(JERNERICS_NAMESPACE, f"job-resource:{snapshot.job_id}"),
        recorded_at=recorded_at or datetime.now(UTC),
        job_id=snapshot.job_id,
        study_name=study_name,
        submission_id=submission_id,
        wall_time_s=snapshot.wall_time_s,
        cpu_time_s=snapshot.cpu_time_s,
        cpu_pct=snapshot.cpu_pct,
        max_rss_mb=snapshot.max_rss_mb,
        ave_rss_mb=snapshot.ave_rss_mb,
        alloc_cpus=snapshot.alloc_cpus,
        req_mem=snapshot.req_mem,
        alloc_tres=snapshot.alloc_tres,
        node_list=snapshot.node_list,
        state=snapshot.state,
        exit_code=snapshot.exit_code,
    )


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

    def fetch_job_resources(self, job_id: str) -> JobResourcesResult:
        """Accounting snapshots for a job id; group/array ids fan out per task."""

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

    def valid_override_keys(self) -> frozenset[str]: ...
