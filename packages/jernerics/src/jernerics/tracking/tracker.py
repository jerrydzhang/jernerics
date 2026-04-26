from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Protocol

from jernerics_proto import Envelope

from .store import TrackingWriter


class Tracker(Protocol):
    def __enter__(self) -> Tracker: ...
    def __exit__(self, *args) -> None: ...
    def log_param(self, key: str, value: bool | float | str) -> None: ...
    def log_metric(self, key: str, value: float, step: int | None = None) -> None: ...
    def log_result(self, key: str, value: Any) -> None: ...
    def log_artifact(self, key: str, local_path: str) -> None: ...
    def close(self) -> None: ...


class ProtobufTracker:
    def __init__(self, study_name: str, trial_id: int, path: Path) -> None:
        self.study_name = study_name
        self.trial_id = trial_id
        self.writer = TrackingWriter(path)

    def __enter__(self) -> ProtobufTracker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def log_param(self, key: str, value: bool | float | str) -> None:
        env = Envelope(
            study_name=self.study_name,
            trial_id=self.trial_id,
            timestamp_ns=time.time_ns(),
        )
        env.param.key = key
        if isinstance(value, bool):
            env.param.value.bool_val = value
        elif isinstance(value, int):
            env.param.value.int_val = value
        elif isinstance(value, float):
            env.param.value.float_val = value
        elif isinstance(value, str):
            env.param.value.string_val = value
        else:
            raise ValueError(f"Unsupported parameter value type: {type(value)}")

        self.writer.write_envelope(env)

    def log_metric(self, key: str, value: float, step: int | None = None) -> None:
        env = Envelope(
            study_name=self.study_name,
            trial_id=self.trial_id,
            timestamp_ns=time.time_ns(),
        )
        env.metric.key = key
        env.metric.value = value
        env.metric.step = step if step is not None else -1

        self.writer.write_envelope(env)

    def log_result(self, key: str, value: Any) -> None:
        env = Envelope(
            study_name=self.study_name,
            trial_id=self.trial_id,
            timestamp_ns=time.time_ns(),
        )
        env.result.key = key
        env.result.value = json.dumps(value)

        self.writer.write_envelope(env)

    def log_artifact(self, key: str, local_path: str) -> None:
        env = Envelope(
            study_name=self.study_name,
            trial_id=self.trial_id,
            timestamp_ns=time.time_ns(),
        )
        env.artifact.key = key
        env.artifact.local_path = local_path

        self.writer.write_envelope(env)

    def close(self) -> None:
        self.writer.close()


class NullTracker:
    def __enter__(self) -> NullTracker:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def log_param(self, key: str, value: bool | float | str) -> None:
        pass

    def log_metric(self, key: str, value: float, step: int | None = None) -> None:
        pass

    def log_result(self, key: str, value: Any) -> None:
        pass

    def log_artifact(self, key: str, local_path: str) -> None:
        pass

    def close(self) -> None:
        pass
