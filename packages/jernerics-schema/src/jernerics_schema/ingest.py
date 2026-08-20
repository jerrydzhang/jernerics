"""Bounded batch ingest request and response models."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .events import TrackingEvent

PROTOCOL_VERSION = 3
"""The wire protocol version defined by this schema package."""

MAX_EVENTS_PER_REQUEST = 100


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
