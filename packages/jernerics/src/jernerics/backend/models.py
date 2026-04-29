from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SweepSpec:
    dag_path: Path
    config_path: Path
    study_name: str
    storage_url: str
    n_trials: int
    dag_relpath: str = ""
    config_relpath: str = ""
    tracking_dir: Path | None = None
    project_name: str | None = None
    server_addr: str | None = None
    max_parallel: int | None = None
    slurm_overrides: dict[str, str] = field(default_factory=dict)


@dataclass
class JobSpec:
    """What to run. Backend-agnostic."""

    command: list[str]
    n_trials: int
    max_parallel: int | None
    log_dir: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class SubmitResult:
    job_id: str
    checker_job_id: str | None = None


@dataclass
class JobInfo:
    job_id: str
    name: str
    status: str
