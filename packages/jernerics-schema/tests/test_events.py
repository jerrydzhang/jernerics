"""Contract tests for the tagged v3 event union."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from jernerics_schema import (
    ArtifactDeclarationEvent,
    Event,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionOutcome,
    ExecutionProgressEvent,
    ExecutionStartEvent,
    FailureKind,
    FlatContext,
    JobSnapshotEvent,
    ManualParamEvent,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrackingEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
)
from pydantic import TypeAdapter, ValidationError

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

VARIANT_CLASSES = (
    SweepSnapshotEvent,
    SubmissionSnapshotEvent,
    JobSnapshotEvent,
    TrialSnapshotEvent,
    ExecutionStartEvent,
    ExecutionHeartbeatEvent,
    ExecutionProgressEvent,
    ExecutionEndEvent,
    ManualParamEvent,
    ValueEvent,
    ArtifactDeclarationEvent,
)

_ADAPTER = TypeAdapter(TrackingEvent)


def _samples() -> list[Event]:
    sweep_id = uuid.uuid4()
    trial_id = uuid.uuid4()
    execution_id = uuid.uuid4()

    def eid() -> uuid.UUID:
        return uuid.uuid4()

    return [
        SweepSnapshotEvent(
            event_id=eid(),
            recorded_at=NOW,
            project="proj",
            sweep_id=sweep_id,
            name="lr-sweep",
            state="active",
        ),
        SubmissionSnapshotEvent(
            event_id=eid(),
            recorded_at=NOW,
            submission_id=uuid.uuid4(),
            sweep_id=sweep_id,
            backend="slurm",
            state=SubmissionState.SUBMITTED,
        ),
        JobSnapshotEvent(
            event_id=eid(),
            recorded_at=NOW,
            job_id=uuid.uuid4(),
            submission_id=uuid.uuid4(),
            scheduler_job_id="123456",
            role="checker",
            state=SubmissionState.SUBMITTED,
        ),
        TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=NOW,
            trial_id=trial_id,
            sweep_id=sweep_id,
            number=3,
            state=TrialState.RUNNING,
            params=FlatContext({"lr": 0.1, "note": "warm"}),
            retry_of_trial_id=None,
            retry_root_trial_id=trial_id,
            retry_index=0,
        ),
        ExecutionStartEvent(
            event_id=eid(),
            recorded_at=NOW,
            execution_id=execution_id,
            trial_id=trial_id,
            hostname="node01",
            host_facts=FlatContext({"gpu": "a100"}),
            started_at=NOW,
        ),
        ExecutionHeartbeatEvent(
            event_id=eid(),
            recorded_at=NOW,
            execution_id=execution_id,
            at=NOW,
        ),
        ExecutionProgressEvent(
            event_id=eid(),
            recorded_at=NOW,
            execution_id=execution_id,
            current=5,
            total=10,
            unit="epoch",
        ),
        ExecutionEndEvent(
            event_id=eid(),
            recorded_at=NOW,
            execution_id=execution_id,
            ended_at=NOW,
            outcome=ExecutionOutcome.FAILURE,
            exit_code=1,
            failure_kind=FailureKind.OOM,
            failure_summary="killed by oom-killer",
        ),
        ManualParamEvent(
            event_id=eid(),
            recorded_at=NOW,
            trial_id=trial_id,
            key="lr",
            value=True,
        ),
        ValueEvent(
            event_id=eid(),
            recorded_at=NOW,
            trial_id=trial_id,
            key="loss",
            step=0,
            value=0.25,
            context=FlatContext({"split": "val"}),
        ),
        ValueEvent(
            event_id=eid(),
            recorded_at=NOW,
            trial_id=trial_id,
            key="pred",
            step=1,
            observation={"expr": "x ** 2"},
        ),
        ArtifactDeclarationEvent(
            event_id=eid(),
            recorded_at=NOW,
            artifact_id=uuid.uuid4(),
            trial_id=trial_id,
            execution_id=execution_id,
            key="curve",
            filename="curve.png",
            content_type="image/png",
            size_bytes=1024,
            sha256="a" * 64,
        ),
    ]


def _heartbeat() -> ExecutionHeartbeatEvent:
    return ExecutionHeartbeatEvent(
        event_id=uuid.uuid4(),
        recorded_at=NOW,
        execution_id=uuid.uuid4(),
        at=NOW,
    )


@pytest.mark.parametrize("event", _samples(), ids=lambda event: event.tag)
def test_event_roundtrip(event: Event) -> None:
    parsed = _ADAPTER.validate_json(event.model_dump_json())
    assert parsed == event
    assert type(parsed) is type(event)


def test_discriminator_dispatches_on_tag() -> None:
    dumped = _samples()[0].model_dump_json()
    assert isinstance(_ADAPTER.validate_json(dumped), SweepSnapshotEvent)


def test_every_variant_has_a_sample() -> None:
    assert {type(event) for event in _samples()} == set(VARIANT_CLASSES)


def test_recorded_at_requires_aware_datetime() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=datetime.fromisoformat("2026-08-19T12:00:00"),
            project="proj",
            sweep_id=uuid.uuid4(),
            name="sweep",
            state="active",
        )


def test_aware_datetimes_normalized_to_utc() -> None:
    aware = NOW.astimezone(timezone(timedelta(hours=2)))
    event = ExecutionHeartbeatEvent(
        event_id=uuid.uuid4(),
        recorded_at=aware,
        execution_id=uuid.uuid4(),
        at=aware,
    )
    assert event.recorded_at.utcoffset() == timedelta(0)
    assert event.at.utcoffset() == timedelta(0)


def test_value_event_requires_exactly_one_payload() -> None:
    kwargs: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "recorded_at": NOW,
        "trial_id": uuid.uuid4(),
        "key": "loss",
        "step": 0,
    }
    with pytest.raises(ValidationError, match="exactly one"):
        ValueEvent(**kwargs)
    with pytest.raises(ValidationError, match="exactly one"):
        ValueEvent(**kwargs, value=1.0, observation={"a": 1})


def test_progress_bounds_enforced() -> None:
    base: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "recorded_at": NOW,
        "execution_id": uuid.uuid4(),
        "unit": "epoch",
    }
    with pytest.raises(ValidationError):
        ExecutionProgressEvent(**base, current=-1, total=10)
    with pytest.raises(ValidationError):
        ExecutionProgressEvent(**base, current=0, total=0)


def test_artifact_sha256_must_be_lowercase_hex() -> None:
    with pytest.raises(ValidationError):
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=NOW,
            artifact_id=uuid.uuid4(),
            trial_id=uuid.uuid4(),
            key="curve",
            filename="curve.png",
            content_type="image/png",
            size_bytes=10,
            sha256="z" * 64,
        )


def test_artifact_source_and_context_defaults_and_values() -> None:
    base: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "recorded_at": NOW,
        "artifact_id": uuid.uuid4(),
        "trial_id": uuid.uuid4(),
        "key": "stdout",
        "filename": "trial-0.stdout",
        "content_type": "text/plain",
        "size_bytes": 3,
    }

    default = ArtifactDeclarationEvent(**base)
    assert default.source == "user"
    assert default.context is None

    system = ArtifactDeclarationEvent(
        **base, source="system", context=FlatContext({"role": "logs"})
    )
    assert system.source == "system"
    assert system.context is not None
    assert system.context.root == {"role": "logs"}

    invalid_source: Any = "scheduler"
    with pytest.raises(ValidationError):
        ArtifactDeclarationEvent(**base, source=invalid_source)


def test_events_are_frozen() -> None:
    event = _heartbeat()
    with pytest.raises(ValidationError):
        event.event_id = uuid.uuid4()


def test_tracking_event_json_schema_covers_all_variants() -> None:
    schema = TypeAdapter(TrackingEvent).json_schema()
    assert schema["discriminator"]["propertyName"] == "tag"
    assert {cls.__name__ for cls in VARIANT_CLASSES} <= set(schema["$defs"])
