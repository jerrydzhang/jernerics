"""Optimizer-neutral domain records and API response records."""

from pydantic import BaseModel, ConfigDict, Field

from .events import UtcDatetime
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


class ValueRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

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
