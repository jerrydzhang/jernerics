"""Client-side v3 tracker: allocates identities and appends tagged events."""

import hashlib
import io
import math
import mimetypes
import os
import socket
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Protocol, Self, TextIO
from uuid import UUID, uuid4

from jernerics_schema import (
    ArtifactDeclarationEvent,
    ArtifactSource,
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
    SweepId,
    TrialId,
    TrialSnapshotEvent,
    TrialState,
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
    def emit_trial_snapshot(
        self,
        *,
        sweep_id: SweepId,
        number: int,
        state: TrialState,
        params: dict[str, ScalarValue],
        objective: float | None = None,
        distributions: FlatContext | None = None,
        attrs: FlatContext | None = None,
        retry_of_trial_id: TrialId | None = None,
        retry_root_trial_id: TrialId | None = None,
        retry_index: int = 0,
    ) -> TrialSnapshotEvent | None: ...
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
        self,
        key: str,
        local_path: str,
        context: dict | None = None,
        *,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> ArtifactDeclarationEvent | None: ...
    def open_artifact(
        self,
        key: str,
        mode: str = "wt",
        context: dict | None = None,
        *,
        filename: str | None = None,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> AbstractContextManager[TextIO | BinaryIO]: ...
    def close(self) -> None: ...


_CHUNK_BYTES = 1024 * 1024


def _copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(source, "rb") as src, open(destination, "wb") as dst:
        for chunk in iter(lambda: src.read(_CHUNK_BYTES), b""):
            dst.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _stage_blob(
    blobs_dir: Path,
    artifact_hex: str,
    local_path: str,
) -> tuple[Path, int, str]:
    final = blobs_dir / f"{artifact_hex}.bin"
    tmp = final.with_name(final.name + ".tmp")
    try:
        size, sha256 = _copy_and_hash(Path(local_path), tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, final)
    return final, size, sha256


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as src:
        for chunk in iter(lambda: src.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def validate_writer_mode(mode: str) -> None:
    """Reject any open_artifact mode other than the two supported ones."""
    if mode not in ("wt", "wb"):
        msg = f"open_artifact mode must be 'wt' or 'wb'; got {mode!r}"
        raise ValueError(msg)


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
                execution_id=self.execution_id,
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
                execution_id=self.execution_id,
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

    def emit_trial_snapshot(
        self,
        *,
        sweep_id: SweepId,
        number: int,
        state: TrialState,
        params: dict[str, ScalarValue],
        objective: float | None = None,
        distributions: FlatContext | None = None,
        attrs: FlatContext | None = None,
        retry_of_trial_id: TrialId | None = None,
        retry_root_trial_id: TrialId | None = None,
        retry_index: int = 0,
    ) -> TrialSnapshotEvent:
        return self._emit(
            TrialSnapshotEvent(
                event_id=uuid4(),
                recorded_at=_now(),
                trial_id=self.trial_id,
                sweep_id=sweep_id,
                number=number,
                state=state,
                params=FlatContext(params),
                objective=objective,
                distributions=distributions,
                attrs=attrs,
                retry_of_trial_id=retry_of_trial_id,
                retry_root_trial_id=(
                    retry_root_trial_id
                    if retry_root_trial_id is not None
                    else self.trial_id
                ),
                retry_index=retry_index,
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
        self,
        key: str,
        local_path: str,
        context: dict | None = None,
        *,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> ArtifactDeclarationEvent:
        manifest = self._require_manifest()
        artifact_id = uuid4()
        display_name = Path(local_path).name
        blobs_dir = manifest.path.parent / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        staged_path, size_bytes, sha256 = _stage_blob(
            blobs_dir,
            artifact_id.hex,
            local_path,
        )
        return self._declare_artifact(
            artifact_id,
            key,
            staged_path,
            display_name,
            size_bytes,
            sha256,
            context,
            source,
            content_type,
        )

    def open_artifact(
        self,
        key: str,
        mode: str = "wt",
        context: dict | None = None,
        *,
        filename: str | None = None,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> AbstractContextManager[TextIO | BinaryIO]:
        validate_writer_mode(mode)
        manifest = self._require_manifest()
        return self._open_artifact_spool(
            manifest,
            key,
            mode,
            context,
            filename if filename is not None else key,
            source,
            content_type,
        )

    def _require_manifest(self) -> ArtifactManifest:
        if self._manifest is None:
            msg = "log_artifact requires a manifest; construct the tracker with one"
            raise RuntimeError(msg)
        return self._manifest

    def _declare_artifact(
        self,
        artifact_id: UUID,
        key: str,
        staged_path: Path,
        filename: str,
        size_bytes: int,
        sha256: str,
        context: dict | None,
        source: ArtifactSource,
        content_type: str | None,
    ) -> ArtifactDeclarationEvent:
        event = ArtifactDeclarationEvent(
            event_id=uuid4(),
            recorded_at=_now(),
            artifact_id=artifact_id,
            trial_id=self.trial_id,
            execution_id=self.execution_id,
            key=key,
            filename=filename,
            content_type=(
                content_type
                if content_type is not None
                else mimetypes.guess_type(filename)[0] or _FALLBACK_CONTENT_TYPE
            ),
            size_bytes=size_bytes,
            sha256=sha256,
            context=self._context(context),
            source=source,
        )
        self._emit(event)
        self._require_manifest().append(
            event.artifact_id.hex, key, str(staged_path), staged=True
        )
        return event

    @contextmanager
    def _open_artifact_spool(
        self,
        manifest: ArtifactManifest,
        key: str,
        mode: str,
        context: dict | None,
        filename: str,
        source: ArtifactSource,
        content_type: str | None,
    ) -> Iterator[TextIO | BinaryIO]:
        artifact_id = uuid4()
        blobs_dir = manifest.path.parent / "blobs"
        blobs_dir.mkdir(parents=True, exist_ok=True)
        spool = blobs_dir / f"{artifact_id.hex}.part"
        # The spool stays open across the yield; every exit path closes it.
        if mode == "wb":
            writer = open(spool, "wb")  # noqa: SIM115
        else:
            writer = open(spool, "w", encoding="utf-8")  # noqa: SIM115
        try:
            yield writer
        except BaseException:
            with suppress(BaseException):
                writer.close()
            spool.unlink(missing_ok=True)
            raise
        writer.close()
        size_bytes, sha256 = _hash_file(spool)
        staged_path = blobs_dir / f"{artifact_id.hex}.bin"
        os.replace(spool, staged_path)
        self._declare_artifact(
            artifact_id,
            key,
            staged_path,
            filename,
            size_bytes,
            sha256,
            context,
            source,
            content_type,
        )

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

    def emit_trial_snapshot(
        self,
        *,
        sweep_id: SweepId,
        number: int,
        state: TrialState,
        params: dict[str, ScalarValue],
        objective: float | None = None,
        distributions: FlatContext | None = None,
        attrs: FlatContext | None = None,
        retry_of_trial_id: TrialId | None = None,
        retry_root_trial_id: TrialId | None = None,
        retry_index: int = 0,
    ) -> None:
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
        self,
        key: str,
        local_path: str,
        context: dict | None = None,
        *,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> None:
        pass

    def open_artifact(
        self,
        key: str,
        mode: str = "wt",
        context: dict | None = None,
        *,
        filename: str | None = None,
        source: ArtifactSource = "user",
        content_type: str | None = None,
    ) -> AbstractContextManager[TextIO | BinaryIO]:
        validate_writer_mode(mode)
        return _null_artifact_sink(mode)

    def close(self) -> None:
        pass


@contextmanager
def _null_artifact_sink(mode: str) -> Iterator[TextIO | BinaryIO]:
    sink: TextIO | BinaryIO = io.StringIO() if mode == "wt" else io.BytesIO()
    yield sink
