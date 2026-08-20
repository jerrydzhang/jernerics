"""Contract tests for the bounded batch ingest models."""

import uuid
from datetime import datetime, timezone

import pytest
from jernerics_schema import (
    PROTOCOL_VERSION,
    ConflictRecord,
    ExecutionHeartbeatEvent,
    IngestError,
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


def test_conflict_record_roundtrip() -> None:
    record = ConflictRecord(
        trial_id=uuid.uuid4(),
        kind="optimizer_terminal_state",
        detail='{"existing":"completed","incoming":"failed"}',
    )
    assert ConflictRecord.model_validate_json(record.model_dump_json()) == record


def test_ingest_error_roundtrip() -> None:
    error = IngestError(
        error="conflict",
        event_index=2,
        event_id=uuid.uuid4(),
        detail="param write-once",
    )
    assert IngestError.model_validate_json(error.model_dump_json()) == error


def test_ingest_error_minimal() -> None:
    error = IngestError(error="validation", detail="unknown trial")
    assert error.event_index is None
    assert error.event_id is None


def test_ingest_response_with_conflicts_roundtrip() -> None:
    response = IngestResponse(
        accepted=2,
        duplicates=1,
        conflicts=(ConflictRecord(trial_id=uuid.uuid4(), kind="k", detail="d"),),
    )
    assert IngestResponse.model_validate_json(response.model_dump_json()) == response
