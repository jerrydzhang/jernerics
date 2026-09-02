"""Flat scalar context values and bounded JSON observation payloads."""

from typing import Annotated, Any

from pydantic import AfterValidator, ConfigDict, Field, RootModel, TypeAdapter

ScalarValue = str | int | float | bool | None

JSON_VALUE_MAX_BYTES = 64 * 1024
"""Maximum UTF-8 byte length of a tracked JSON observation when encoded."""

_json_object_adapter = TypeAdapter(dict[str, Any])


class FlatContext(RootModel[dict[str, ScalarValue]]):
    """A flat ``str -> scalar`` mapping; nested containers are rejected."""

    model_config = ConfigDict(frozen=True)

    root: dict[str, ScalarValue] = Field(default_factory=dict)


def validate_json_size(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """Reject JSON objects whose canonical encoding exceeds the size limit."""
    if value is None:
        return None
    encoded = _json_object_adapter.dump_json(value)
    if len(encoded) > JSON_VALUE_MAX_BYTES:
        msg = (
            f"observation encodes to {len(encoded)} bytes of JSON, exceeding "
            f"the {JSON_VALUE_MAX_BYTES}-byte limit; write bulky payloads as an "
            "artifact instead: with tracker.open_artifact(key, 'wt') as f: "
            "json.dump(value, f)"
        )
        raise ValueError(msg)
    return value


Observation = Annotated[dict[str, Any] | None, AfterValidator(validate_json_size)]
"""An arbitrary JSON object payload, bounded to ``JSON_VALUE_MAX_BYTES``."""
