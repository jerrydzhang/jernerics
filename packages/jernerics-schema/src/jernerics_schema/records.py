"""Optimizer-neutral domain records and API response records."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .events import ArtifactSource, UtcDatetime
from .ids import ArtifactId, ExecutionId, JobId, SubmissionId, SweepId, TrialId
from .lifecycle import ExecutionOutcome, FailureKind, SubmissionState, TrialState
from .lineage import RetryLineage
from .scalars import FlatContext, Observation, ScalarValue


class SweepRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    sweep_id: SweepId
    project: str
    name: str
    state: str


class SubmissionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    submission_id: SubmissionId
    sweep_id: SweepId
    backend: str
    state: SubmissionState
    submitted_at: UtcDatetime | None = None
    expected_trials: int | None = Field(default=None, ge=1)
    git_hash: str | None = None
    config_source: str | None = None


class JobRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    job_id: JobId
    submission_id: SubmissionId
    scheduler_job_id: str
    role: str = "trials"
    state: SubmissionState


class TrialRecord(RetryLineage):
    model_config = ConfigDict(frozen=True)

    trial_id: TrialId
    sweep_id: SweepId
    number: int = Field(ge=0)
    state: TrialState
    params: FlatContext = Field(default_factory=FlatContext)
    objective: float | None = None
    distributions: FlatContext | None = None
    attrs: FlatContext | None = None


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: ExecutionId
    trial_id: TrialId
    hostname: str
    started_at: UtcDatetime
    ended_at: UtcDatetime | None = None
    outcome: ExecutionOutcome | None = None
    exit_code: int | None = None
    failure_kind: FailureKind | None = None
    last_heartbeat_ns: int | None = None
    last_observation_ns: int | None = None
    monitoring: Literal["active", "quiet", "stale", "ended", "unknown"] | None = None


class ValueRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    execution_id: ExecutionId | None = None
    trial_id: TrialId
    key: str
    step: int = Field(ge=0)
    value: ScalarValue | None = None
    observation: Observation = None
    context: FlatContext | None = None


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    artifact_id: ArtifactId
    trial_id: TrialId
    execution_id: ExecutionId | None = None
    key: str
    filename: str
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context: FlatContext | None = None
    source: ArtifactSource = "user"
    received_ns: int | None = None


class TrialParamRecord(BaseModel):
    """One flat param row (sampled or manual) of a trial."""

    model_config = ConfigDict(frozen=True)

    trial_id: TrialId
    kind: Literal["sampled", "manual"]
    key: str
    value: ScalarValue = None


class TrialLineageRecord(BaseModel):
    """Retry-family position of a trial, for lineage views."""

    model_config = ConfigDict(frozen=True)

    trial_id: TrialId
    retry_of_trial_id: TrialId | None = None
    retry_root_trial_id: TrialId
    retry_index: int = Field(default=0, ge=0)
    number: int = Field(ge=0)
    sweep_id: SweepId


class ValueCatalogRecord(BaseModel):
    """Discovered value key: kind, volume, and how many trials logged it."""

    model_config = ConfigDict(frozen=True)

    key: str
    kind: Literal["scalar", "json"]
    n_points: int = Field(ge=0)
    latest_step: int = Field(ge=0)
    n_trials: int = Field(ge=0)


class ProvenanceRecord(BaseModel):
    """Submission-level provenance facts for a sweep."""

    model_config = ConfigDict(frozen=True)

    submission_id: SubmissionId
    sweep_id: SweepId
    backend: str
    submitted_at_ns: int | None = None
    expected_trials: int | None = Field(default=None, ge=1)
    git_hash: str | None = None
    config_source: str | None = None


class JobResourceRecord(BaseModel):
    """Scheduler accounting facts for one job, captured post-hoc."""

    model_config = ConfigDict(frozen=True)

    job_id: str
    study_name: str | None = None
    submission_id: str | None = None
    wall_time_s: float | None = None
    cpu_time_s: float | None = None
    cpu_pct: float | None = None
    max_rss_mb: float | None = None
    ave_rss_mb: float | None = None
    alloc_cpus: int | None = None
    req_mem: str | None = None
    alloc_tres: str | None = None
    node_list: str | None = None
    state: str | None = None
    exit_code: str | None = None
    recorded_at: UtcDatetime
