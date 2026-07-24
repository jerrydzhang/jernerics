import json
import math
import numbers
import sys
import time
from pathlib import Path
from typing import Any, Protocol, Self

from .artifact_manifest import ArtifactManifest
from .jsonl_io import TrackingWriter


class Tracker(Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(self, *args) -> None: ...
    def log_param(self, key: str, value: bool | float | str) -> None: ...
    def log_value(
        self,
        key: str,
        value: float,
        step: int | None = None,
        context: dict | None = None,
    ) -> None: ...
    def log_json(
        self,
        key: str,
        value: Any,
        step: int | None = None,
        context: dict | None = None,
    ) -> None: ...
    def log_artifact(
        self, key: str, local_path: str, context: dict | None = None
    ) -> None: ...
    def log_sweep_meta(self, git_hash: str | None, config: str) -> None: ...
    def close(self) -> None: ...


class JsonlTracker:
    def __init__(
        self,
        project: str,
        study_name: str,
        trial_id: int,
        path: Path,
        *,
        manifest_path: Path | None = None,
        run_id: int = 0,
    ) -> None:
        self.project = project
        self.study_name = study_name
        self.trial_id = trial_id
        self.run_id = run_id
        self._seq = 0
        self.writer = TrackingWriter(path)
        self._manifest = ArtifactManifest(manifest_path) if manifest_path else None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def _make_envelope(self) -> dict:
        return {
            "project": self.project,
            "study_name": self.study_name,
            "trial_id": self.trial_id,
            "run_id": self.run_id,
            "timestamp_ns": time.time_ns(),
            "seq": self._next_seq(),
        }

    @staticmethod
    def _serialize_context(context: dict | None) -> str:
        return json.dumps(context or {}, sort_keys=True, separators=(",", ":"))

    def log_param(self, key: str, value: bool | float | str) -> None:
        env = self._make_envelope()
        if isinstance(value, bool):
            typed = {"bool_val": value}
        elif isinstance(value, int):
            typed = {"int_val": value}
        elif isinstance(value, float):
            typed = {"float_val": value}
        elif isinstance(value, str):
            typed = {"string_val": value}
        else:
            raise TypeError(f"Unsupported parameter value type: {type(value)}")

        env["param"] = {"key": key, "value": typed}
        self.writer.write_envelope(env)

    def log_value(
        self,
        key: str,
        value: float,
        step: int | None = None,
        context: dict | None = None,
    ) -> None:
        if isinstance(value, bool):
            scalar: float | None = float(value)
        elif isinstance(value, numbers.Real):
            scalar = float(value)
        else:
            raise TypeError(f"Unsupported value type: {type(value)}")

        if scalar is not None and (math.isnan(scalar) or math.isinf(scalar)):
            print(
                f"jernerics: {key} value {scalar} is NaN/Inf; storing NULL",
                file=sys.stderr,
            )
            scalar = None

        env = self._make_envelope()
        env["value"] = {
            "key": key,
            "value": scalar,
            "step": step,
            "context": self._serialize_context(context),
        }
        self.writer.write_envelope(env)

    def log_json(
        self,
        key: str,
        value: Any,
        step: int | None = None,
        context: dict | None = None,
    ) -> None:
        env = self._make_envelope()
        env["value"] = {
            "key": key,
            "value_json": json.dumps(value),
            "step": step,
            "context": self._serialize_context(context),
        }
        self.writer.write_envelope(env)

    def log_sweep_meta(self, git_hash: str | None, config: str) -> None:
        env = self._make_envelope()
        env["sweep_meta"] = {"git_hash": git_hash, "config": config}
        self.writer.write_envelope(env)

    def log_artifact(
        self, key: str, local_path: str, context: dict | None = None
    ) -> None:
        env = self._make_envelope()
        env["artifact"] = {
            "key": key,
            "filename": Path(local_path).name,
            "context": self._serialize_context(context),
        }
        self.writer.write_envelope(env)

        if self._manifest:
            self._manifest.append(key, local_path)

    def close(self) -> None:
        env = self._make_envelope()
        env["trial_end"] = {}
        self.writer.write_envelope(env)
        self.writer.close()


class NullTracker:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def log_param(self, key: str, value: bool | float | str) -> None:
        pass

    def log_value(
        self,
        key: str,
        value: float,
        step: int | None = None,
        context: dict | None = None,
    ) -> None:
        pass

    def log_json(
        self,
        key: str,
        value: Any,
        step: int | None = None,
        context: dict | None = None,
    ) -> None:
        pass

    def log_artifact(
        self, key: str, local_path: str, context: dict | None = None
    ) -> None:
        pass

    def log_sweep_meta(self, git_hash: str | None, config: str) -> None:
        pass

    def close(self) -> None:
        pass
