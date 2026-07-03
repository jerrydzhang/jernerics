from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SweepSubmission:
    trial_path: Path
    config_path: Path
    study_name: str
    storage_url: str
    n_trials: int
    trial_relpath: str = ""
    config_relpath: str = ""
    tracking_dir: Path | None = None
    project_name: str | None = None
    server_addr: str | None = None
    max_parallel: int | None = None
    backend_overrides: dict[str, str] = field(default_factory=dict)
    grid: dict[str, list] | None = None


@dataclass
class JobSpec:
    """What to run. Backend-agnostic."""

    command: list[str]
    n_trials: int
    max_parallel: int | None
    log_dir: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class JobSubmission:
    job_id: str
    output_pattern: str | None = None
    error_pattern: str | None = None
    n_trials: int = 0


@dataclass
class SubmitResult:
    """A list because Slurm yields multiple jobs (array + post-hook, each with
    its own log path) while Pueue yields one group. Normalizing both to a list
    lets the backend save them unconditionally instead of special-casing per
    scheduler.
    """

    submissions: list[JobSubmission]


@dataclass
class JobInfo:
    job_id: str
    name: str
    status: str
