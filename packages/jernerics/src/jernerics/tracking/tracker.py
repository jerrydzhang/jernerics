"""Client-side v3 tracker: allocates identities and appends tagged events."""

import hashlib
import math
import mimetypes
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, Self
from uuid import uuid4

from jernerics_schema import (
    ArtifactDeclarationEvent,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionId,
    ExecutionOutcome,
    ExecutionProgressEvent,
    ExecutionStartEvent,
    FailureKind,
    FlatContext,
    ManualParamEvent,
    ScalarValue,
    TrialId,
    ValueEvent,
)

from .artifact_manifest import ArtifactManifest
from .jsonl_io import TrackingWriter

_FALLBACK_CONTENT_TYPE = "application/octet-stream"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Tracker(Protocol):
    def __enter__(self) -> Self: ...
    def __exit__(self, *args) -> None: ...
    def log_param(self, key: str, value: ScalarValue) -> ManualParamEvent | None: ...
    def log_value(
        self,
        key: str,
        value: float | bool | str | None,
        *,
        step: int | None = None,
        context: dict[str, ScalarValue] | None = None,
    ) -> ValueEvent | None: ...
    def log_json(
        self,
        key: str,
        observation: dict,
        *,
        step: int | None = None,
        context: dict[str, ScalarValue] | None = None,
    ) -> ValueEvent | None: ...
    def set_progress(
        self, current: int, total: int, unit: str
    ) -> ExecutionProgressEvent | None: ...
    def emit_heartbeat(
        self, at: datetime | None = None
    ) -> ExecutionHeartbeatEvent | None: ...
    def emit_execution_start(
        self, hostname: str | None = None
    ) -> ExecutionStartEvent | None: ...
    def emit_execution_end(
        self,
        outcome: ExecutionOutcome,
        *,
        exit_code: int | None = None,
        failure_kind: FailureKind | None = None,
        failure_summary: str | None = None,
    ) -> ExecutionEndEvent | None: ...
    def log_artifact(
        self, key: str, local_path: str, context: dict | None = None
    ) -> ArtifactDeclarationEvent | None: ...
    def close(self) -> None: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class JsonlTracker:
    """Appends v3 tagged events, one per JSONL line, to a trial's event log."""

    def __init__(
        self,
        path: Path,
        trial_id: TrialId,
        execution_id: ExecutionId | None = None,
        *,
        writer: TrackingWriter | None = None,
        manifest: ArtifactManifest | None = None,
    ) -> None:
        self.path = path
        self.trial_id = trial_id
        self.execution_id = execution_id
        self.writer = writer if writer is not None else TrackingWriter(path)
        self._manifest = manifest
        self._next_step: dict[str, int] = {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _emit(self, event):
        self.writer.write_event(event)
        return event

    def _require_execution_id(self, method: str) -> ExecutionId:
        if self.execution_id is None:
            msg = f"{method} requires an execution_id; construct the tracker with one"
            raise RuntimeError(msg)
        return self.execution_id

    def _step_for(self, key: str, step: int | None) -> int:
        if step is None:
            step = self._next_step.get(key, 0)
        self._next_step[key] = max(self._next_step.get(key, 0), step + 1)
        return step

    @staticmethod
    def _context(context: dict | None) -> FlatContext | None:
        if context is None:
            return None
        return FlatContext(context)

    def log_param(self, key: str, value: ScalarValue) -> ManualParamEvent:
        return self._emit(
            ManualParamEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                trial_id=self.trial_id,
                key=key,
                value=value,
            )
        )

    def log_value(
        self,
        key: str,
        value: float | bool | str | None,
        *,
        step: int | None = None,
        context: dict[str, ScalarValue] | None = None,
    ) -> ValueEvent:
        if isinstance(value, float) and not math.isfinite(value):
            msg = (
                f"log_value key {key!r}: non-finite value {value!r} cannot be "
                "represented in a v3 event"
            )
            raise ValueError(msg)
        return self._emit(
            ValueEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                trial_id=self.trial_id,
                key=key,
                step=self._step_for(key, step),
                value=value,
                context=self._context(context),
            )
        )

    def log_json(
        self,
        key: str,
        observation: dict,
        *,
        step: int | None = None,
        context: dict[str, ScalarValue] | None = None,
    ) -> ValueEvent:
        return self._emit(
            ValueEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                trial_id=self.trial_id,
                key=key,
                step=self._step_for(key, step),
                observation=observation,
                context=self._context(context),
            )
        )

    def set_progress(
        self, current: int, total: int, unit: str
    ) -> ExecutionProgressEvent:
        return self._emit(
            ExecutionProgressEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                execution_id=self._require_execution_id("set_progress"),
                current=current,
                total=total,
                unit=unit,
            )
        )

    def emit_heartbeat(self, at: datetime | None = None) -> ExecutionHeartbeatEvent:
        return self._emit(
            ExecutionHeartbeatEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                execution_id=self._require_execution_id("emit_heartbeat"),
                at=at if at is not None else _now(),
            )
        )

    def emit_execution_start(self, hostname: str | None = None) -> ExecutionStartEvent:
        return self._emit(
            ExecutionStartEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                execution_id=self._require_execution_id("emit_execution_start"),
                trial_id=self.trial_id,
                hostname=hostname if hostname is not None else socket.gethostname(),
                started_at=_now(),
            )
        )

    def emit_execution_end(
        self,
        outcome: ExecutionOutcome,
        *,
        exit_code: int | None = None,
        failure_kind: FailureKind | None = None,
        failure_summary: str | None = None,
    ) -> ExecutionEndEvent:
        return self._emit(
            ExecutionEndEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                execution_id=self._require_execution_id("emit_execution_end"),
                ended_at=_now(),
                outcome=outcome,
                exit_code=exit_code,
                failure_kind=failure_kind,
                failure_summary=failure_summary,
            )
        )

    def log_artifact(
        self, key: str, local_path: str, context: dict | None = None
    ) -> ArtifactDeclarationEvent:
        local = Path(local_path)
        if context is not None:
            # Declarations carry no context on the wire; validate anyway so
            # invalid input fails here instead of being silently dropped.
            FlatContext(context)
        event = ArtifactDeclarationEvent(
            event_id=uuid4(),
            recorded_at=_now(),
            artifact_id=uuid4(),
            trial_id=self.trial_id,
            execution_id=self.execution_id,
            key=key,
            filename=local.name,
            content_type=mimetypes.guess_type(local.name)[0] or _FALLBACK_CONTENT_TYPE,
            size_bytes=local.stat().st_size,
            sha256=_sha256_file(local),
        )
        self._emit(event)
        if self._manifest is not None:
            self._manifest.append(key, str(local_path))
        return event

    def close(self) -> None:
        self.writer.close()


class NullTracker:
    """No-op tracker matching the v3 surface."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def log_param(self, key: str, value: ScalarValue) -> None:
        pass

    def log_value(
        self,
        key: str,
        value: float | bool | str | None,
        *,
        step: int | None = None,
        context: dict[str, ScalarValue] | None = None,
    ) -> None:
        pass

    def log_json(
        self,
        key: str,
        observation: dict,
        *,
        step: int | None = None,
        context: dict[str, ScalarValue] | None = None,
    ) -> None:
        pass

    def set_progress(self, current: int, total: int, unit: str) -> None:
        pass

    def emit_heartbeat(self, at: datetime | None = None) -> None:
        pass

    def emit_execution_start(self, hostname: str | None = None) -> None:
        pass

    def emit_execution_end(
        self,
        outcome: ExecutionOutcome,
        *,
        exit_code: int | None = None,
        failure_kind: FailureKind | None = None,
        failure_summary: str | None = None,
    ) -> None:
        pass

    def log_artifact(
        self, key: str, local_path: str, context: dict | None = None
    ) -> None:
        pass

    def close(self) -> None:
        pass
