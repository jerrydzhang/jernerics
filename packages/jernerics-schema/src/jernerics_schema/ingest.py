"""Bounded batch ingest request and response models."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .events import TrackingEvent
from .ids import EventId, TrialId

PROTOCOL_VERSION = 3
"""The wire protocol version defined by this schema package."""

MAX_EVENTS_PER_REQUEST = 100


class ConflictRecord(BaseModel):
    """A reconciliation conflict detected while applying a batch."""

    model_config = ConfigDict(frozen=True)

    trial_id: TrialId
    kind: str
    detail: str


class IngestError(BaseModel):
    """Structured failure body for a rejected batch."""

    model_config = ConfigDict(frozen=True)

    error: str
    event_index: int | None = None
    event_id: EventId | None = None
    detail: str = ""


class IngestRequest(BaseModel):
    """One batch of events shipped to the tracking server."""

    model_config = ConfigDict(frozen=True)

    protocol_version: int
    events: list[TrackingEvent] = Field(max_length=MAX_EVENTS_PER_REQUEST)

    @field_validator("protocol_version")
    @classmethod
    def _match_protocol_version(cls, value: int) -> int:
        if value != PROTOCOL_VERSION:
            msg = (
                f"unsupported protocol_version {value}; "
                f"this wire contract defines PROTOCOL_VERSION={PROTOCOL_VERSION}"
            )
            raise ValueError(msg)
        return value


class IngestResponse(BaseModel):
    """Server acknowledgement for one ingested batch."""

    model_config = ConfigDict(frozen=True)

    accepted: int = Field(ge=0)
    duplicates: int = Field(default=0, ge=0)
    conflicts: tuple[ConflictRecord, ...] = ()
