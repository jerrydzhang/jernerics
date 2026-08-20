"""Contract tests for the bounded batch ingest models."""

import uuid
from datetime import datetime, timezone

import pytest
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionHeartbeatEvent,
    IngestRequest,
    IngestResponse,
    TrackingEvent,
)
from jernerics_schema.ingest import MAX_EVENTS_PER_REQUEST
from pydantic import ValidationError

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _heartbeat() -> ExecutionHeartbeatEvent:
    return ExecutionHeartbeatEvent(
        event_id=uuid.uuid4(),
        recorded_at=NOW,
        execution_id=uuid.uuid4(),
        at=NOW,
    )


def test_ingest_request_accepts_current_protocol() -> None:
    request = IngestRequest(protocol_version=PROTOCOL_VERSION, events=[_heartbeat()])
    assert request.events[0].tag == "execution_heartbeat"


@pytest.mark.parametrize("version", [2, 4])
def test_ingest_request_rejects_other_protocols(version: int) -> None:
    with pytest.raises(ValidationError, match="protocol_version"):
        IngestRequest(protocol_version=version, events=[])


def test_ingest_request_event_bound() -> None:
    events: list[TrackingEvent] = [_heartbeat() for _ in range(MAX_EVENTS_PER_REQUEST)]
    IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
    with pytest.raises(ValidationError):
        IngestRequest(protocol_version=PROTOCOL_VERSION, events=[*events, _heartbeat()])


def test_ingest_request_roundtrip() -> None:
    request = IngestRequest(protocol_version=PROTOCOL_VERSION, events=[_heartbeat()])
    assert IngestRequest.model_validate_json(request.model_dump_json()) == request


def test_ingest_response_defaults() -> None:
    assert IngestResponse(accepted=3).duplicates == 0
