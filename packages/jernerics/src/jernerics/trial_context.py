import json
import os
from pathlib import Path
from typing import Any, Protocol

from jernerics.tracking.tracker import JsonlTracker

TRIAL_CONFIG_ENV = "JERNERICS_TRIAL_CONFIG"
TRACKING_DIR_ENV = "JERNERICS_TRACKING_DIR"
PROJECT_NAME_ENV = "JERNERICS_PROJECT_NAME"
STUDY_NAME_ENV = "JERNERICS_STUDY_NAME"
TRIAL_NUMBER_ENV = "JERNERICS_TRIAL_NUMBER"
RUN_ID_ENV = "JERNERICS_RUN_ID"


class TrackerProtocol(Protocol):
    def log_param(self, key: str, value: str) -> None: ...
    def log_value(self, key: str, value: float, step: int) -> None: ...
    def log_text(self, key: str, value: str) -> None: ...
    def finish(self, results: dict[str, Any]) -> None: ...


class ConsoleTracker:
    """Standalone tracker used when a trial script runs by hand (no jernerics
    job environment). Mirrors the :class:`JsonlTracker` surface so trial
    scripts execute unchanged outside the runner; each call prints to stdout."""

    def log_param(self, key: str, value: object) -> None:
        print(f"param: {key}={value}")

    def log_value(
        self,
        key: str,
        value: float,
        step: int | None = None,
        context: dict | None = None,
    ) -> None:
        print(f"[step {step}] {key}={value}")

    def log_text(self, key: str, value: str) -> None:
        print(f"[text] {key}={value}")

    def log_json(
        self,
        key: str,
        value: Any,
        step: int | None = None,
        context: dict | None = None,
    ) -> None:
        print(f"[step {step}] {key}={json.dumps(value)}")

    def log_artifact(
        self, key: str, local_path: str, context: dict | None = None
    ) -> None:
        print(f"[artifact] {key}={local_path}")

    def log_sweep_meta(self, git_hash: str | None, config: str) -> None:
        print(f"[sweep] git={git_hash}")

    def finish(self, results: dict[str, Any]) -> None:
        print("results:")
        for key, value in results.items():
            print(f"  {key}={value}")


class _TrialTracker(JsonlTracker):
    def log_text(self, key: str, value: str) -> None:
        self.log_json(key, value)

    def finish(self, results: dict[str, Any]) -> None:
        self.log_json("results", results)
        self.writer.close()


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
    project_name = _required_env(PROJECT_NAME_ENV)
    study_name = _required_env(STUDY_NAME_ENV)
    trial_number = _required_int(TRIAL_NUMBER_ENV)
    run_id = _optional_int(RUN_ID_ENV, 0)

    root = Path(tracking_dir)
    events_dir = root / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    artifacts_dir = root / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    return _TrialTracker(
        project_name,
        study_name,
        trial_number,
        events_dir / f"{trial_number}.jsonl",
        manifest_path=artifacts_dir / f"{trial_number}.manifest",
        run_id=run_id,
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        raise RuntimeError(f"{name} is required when running as a jernerics job")
    return value


def _required_int(name: str) -> int:
    value = _required_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _optional_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
