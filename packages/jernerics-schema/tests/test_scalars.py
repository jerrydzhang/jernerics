"""Contract tests for flat scalar contexts and bounded observations."""

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from jernerics_schema import JSON_VALUE_MAX_BYTES, FlatContext, ValueEvent
from pydantic import TypeAdapter, ValidationError

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)

_DUMP = TypeAdapter(dict[str, Any])


def _observation_with_encoded_size(size: int) -> dict[str, Any]:
    """Build an observation whose canonical JSON encoding is exactly `size` bytes."""
    assert size >= 10
    observation = {"pad": "x" * (size - 10)}
    assert len(_DUMP.dump_json(observation)) == size
    return observation


def _value_event(**kwargs: Any) -> ValueEvent:
    base: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "recorded_at": NOW,
        "trial_id": uuid.uuid4(),
        "key": "obs",
        "step": 0,
    }
    base.update(kwargs)
    return ValueEvent(**base)


@pytest.mark.parametrize("value", ["text", 3, 2.5, True, None])
def test_flat_context_accepts_scalars(value: Any) -> None:
    assert FlatContext({"k": value}).root == {"k": value}


@pytest.mark.parametrize(
    "value",
    [{"nested": 1}, [1, 2], {"nested": [None]}, {"nested": {"deeper": 2}}],
)
def test_flat_context_rejects_nested_containers(value: Any) -> None:
    with pytest.raises(ValidationError):
        FlatContext({"k": value})


def test_value_event_context_rejects_nested_containers() -> None:
    with pytest.raises(ValidationError):
        _value_event(value=1.0, context={"k": [1]})


def test_observation_at_limit_passes() -> None:
    observation = _observation_with_encoded_size(JSON_VALUE_MAX_BYTES)
    event = _value_event(observation=observation)
    assert event.observation == observation


def test_observation_over_limit_rejected() -> None:
    observation = _observation_with_encoded_size(JSON_VALUE_MAX_BYTES + 1)
    with pytest.raises(
        ValidationError,
        match=r"with tracker\.open_artifact\(key, 'wt'\)",
    ):
        _value_event(observation=observation)
