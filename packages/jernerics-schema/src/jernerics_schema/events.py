"""Tagged v3 tracking events — the wire contract between client and server."""

from datetime import datetime, timezone
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from .ids import ArtifactId, EventId, ExecutionId, SubmissionId, SweepId, TrialId
from .lifecycle import ExecutionOutcome, FailureKind, SubmissionState, TrialState
from .lineage import RetryLineage
from .scalars import FlatContext, Observation, ScalarValue


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        msg = "datetime must be timezone-aware"
        raise ValueError(msg)
    return value.astimezone(timezone.utc)


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_aware_utc)]
"""A datetime that must be timezone-aware and is normalized to UTC."""


class Event(BaseModel):
    """Base for all tracking events; never travels on the wire alone."""

    model_config = ConfigDict(frozen=True)

    event_id: EventId
    recorded_at: UtcDatetime
    tag: str


class SweepSnapshotEvent(Event):
    tag: Literal["sweep_snapshot"] = "sweep_snapshot"
    project: str
    sweep_id: SweepId
    name: str
    state: str


class SubmissionSnapshotEvent(Event):
    tag: Literal["submission_snapshot"] = "submission_snapshot"
    submission_id: SubmissionId
    sweep_id: SweepId
    backend: str
    state: SubmissionState


class TrialSnapshotEvent(RetryLineage, Event):
    tag: Literal["trial_snapshot"] = "trial_snapshot"
    trial_id: TrialId
    sweep_id: SweepId
    number: int = Field(ge=0)
    state: TrialState
    params: FlatContext = Field(default_factory=FlatContext)


class ExecutionStartEvent(Event):
    tag: Literal["execution_start"] = "execution_start"
    execution_id: ExecutionId
    trial_id: TrialId
    hostname: str
    host_facts: FlatContext | None = None
    started_at: UtcDatetime


class ExecutionHeartbeatEvent(Event):
    tag: Literal["execution_heartbeat"] = "execution_heartbeat"
    execution_id: ExecutionId
    at: UtcDatetime


class ExecutionProgressEvent(Event):
    tag: Literal["execution_progress"] = "execution_progress"
    execution_id: ExecutionId
    current: int = Field(ge=0)
    total: int = Field(gt=0)
    unit: str


class ExecutionEndEvent(Event):
    tag: Literal["execution_end"] = "execution_end"
    execution_id: ExecutionId
    ended_at: UtcDatetime
    outcome: ExecutionOutcome
    exit_code: int | None = None
    failure_kind: FailureKind | None = None
    failure_summary: str | None = Field(default=None, max_length=2000)


class ManualParamEvent(Event):
    tag: Literal["manual_param"] = "manual_param"
    trial_id: TrialId
    key: str
    value: ScalarValue


class ValueEvent(Event):
    tag: Literal["value"] = "value"
    trial_id: TrialId
    key: str
    step: int = Field(ge=0)
    value: ScalarValue | None = None
    observation: Observation = None
    context: FlatContext | None = None

    @model_validator(mode="after")
    def _require_exactly_one_payload(self) -> Self:
        if (self.value is None) == (self.observation is None):
            msg = "exactly one of 'value' or 'observation' is required"
            raise ValueError(msg)
        return self


class ArtifactDeclarationEvent(Event):
    tag: Literal["artifact_declaration"] = "artifact_declaration"
    artifact_id: ArtifactId
    trial_id: TrialId
    execution_id: ExecutionId | None = None
    key: str
    filename: str
    content_type: str
    size_bytes: int = Field(ge=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


TrackingEvent = Annotated[
    SweepSnapshotEvent
    | SubmissionSnapshotEvent
    | TrialSnapshotEvent
    | ExecutionStartEvent
    | ExecutionHeartbeatEvent
    | ExecutionProgressEvent
    | ExecutionEndEvent
    | ManualParamEvent
    | ValueEvent
    | ArtifactDeclarationEvent,
    Field(discriminator="tag"),
]
"""Discriminated union of every v3 tracking event, dispatched on ``tag``."""
