import json
import os
import uuid
from pathlib import Path
from typing import Protocol

from jernerics_schema import ArtifactSource

from jernerics.tracking.artifact_manifest import ArtifactManifest
from jernerics.tracking.tracker import JsonlTracker

TRIAL_CONFIG_ENV = "JERNERICS_TRIAL_CONFIG"
TRACKING_DIR_ENV = "JERNERICS_TRACKING_DIR"
PROJECT_NAME_ENV = "JERNERICS_PROJECT_NAME"
STUDY_NAME_ENV = "JERNERICS_STUDY_NAME"
TRIAL_NUMBER_ENV = "JERNERICS_TRIAL_NUMBER"
SWEEP_ID_ENV = "JERNERICS_SWEEP_ID"
TRIAL_ID_ENV = "JERNERICS_TRIAL_ID"
EXECUTION_ID_ENV = "JERNERICS_EXECUTION_ID"

type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)


class TrackerProtocol(Protocol):
    def log_param(self, key: str, value: str | float | bool) -> None: ...

    def log_value(
        self, key: str, value: JsonValue, *, step: int | None = None
    ) -> None: ...

    def log_artifact(
        self,
        key: str,
        path: str,
        *,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> None: ...

    def finish(self, results: dict[str, JsonValue]) -> None: ...


class ConsoleTracker:
    """Trial tracker for standalone runs; prints each observation to stdout."""

    def log_param(self, key: str, value: str | float | bool) -> None:
        print(f"param: {key}={value}")

    def log_value(self, key: str, value: JsonValue, *, step: int | None = None) -> None:
        encoded = json.dumps(value)
        if step is None:
            print(f"[value] {key}={encoded}")
        else:
            print(f"[step {step}] {key}={encoded}")

    def log_artifact(
        self,
        key: str,
        path: str,
        *,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> None:
        print(f"[artifact] {key}={path}")

    def finish(self, results: dict[str, JsonValue]) -> None:
        print("results:")
        for key, value in results.items():
            print(f"  {key}={value}")


class _JobTracker:
    """Trial tracker for jernerics job runs; composes the JSONL tracking backend."""

    def __init__(self, tracker: JsonlTracker) -> None:
        self._tracker = tracker

    def log_param(self, key: str, value: str | float | bool) -> None:
        self._tracker.log_param(key, value)

    def log_value(self, key: str, value: JsonValue, *, step: int | None = None) -> None:
        if isinstance(value, bool | int | float | str):
            self._tracker.log_value(key, value, step=step)
        elif isinstance(value, dict):
            self._tracker.log_json(key, value, step=step)
        else:
            msg = f"cannot track {type(value).__name__} observation for {key!r}"
            raise TypeError(msg)

    def log_artifact(
        self,
        key: str,
        path: str,
        *,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> None:
        self._tracker.log_artifact(key, path, source=source, content_type=content_type)

    def finish(self, results: dict[str, JsonValue]) -> None:
        self._tracker.log_json("results", results)
        self._tracker.writer.close()


def is_job() -> bool:
    return TRIAL_CONFIG_ENV in os.environ


def trial_config(defaults: dict | None = None) -> dict:
    if not is_job():
        if defaults is None:
            raise ValueError("defaults is required outside a jernerics job")
        return defaults

    path = Path(os.environ[TRIAL_CONFIG_ENV])
    try:
        with path.open() as f:
            data = json.load(f)
    except OSError as exc:
        raise RuntimeError(f"Unable to read trial config from {path}") from exc

    if not isinstance(data, dict):
        raise TypeError("trial config JSON must contain an object")
    return data


def trial_tracker() -> TrackerProtocol:
    if not is_job():
        return ConsoleTracker()

    tracking_dir = _required_env(TRACKING_DIR_ENV)
    trial_number = _required_int(TRIAL_NUMBER_ENV)
    trial_id = _required_uuid(TRIAL_ID_ENV)
    execution_id = _required_uuid(EXECUTION_ID_ENV)

    root = Path(tracking_dir)
    events_dir = root / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    return _JobTracker(
        JsonlTracker(
            events_dir / f"{trial_number}.jsonl",
            trial_id,
            execution_id,
            manifest=ArtifactManifest(artifacts_dir / f"{trial_number}.manifest"),
        )
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"{name} is required inside a jernerics job")
    return value


def _required_int(name: str) -> int:
    value = _required_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _required_uuid(name: str) -> uuid.UUID:
    value = _required_env(name)
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a UUID") from exc
