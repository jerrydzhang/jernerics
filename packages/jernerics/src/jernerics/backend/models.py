from dataclasses import dataclass, field


@dataclass
class JobSpec:
    """What to run. Backend-agnostic."""

    command: list[str]
    n_trials: int
    max_parallel: int | None
    log_dir: str
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class JobInfo:
    job_id: str
    name: str
    status: str
