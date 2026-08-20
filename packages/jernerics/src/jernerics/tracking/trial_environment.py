"""Runner-side trial environment: execution lifecycle, heartbeats, live shipping."""

import sys
import threading
from pathlib import Path
from typing import Self
from uuid import uuid4

from jernerics_schema import (
    ExecutionEndEvent,
    ExecutionId,
    ExecutionOutcome,
    FailureKind,
    TrialId,
)

from jernerics.tracking import Tracker
from jernerics.tracking.artifact_manifest import ArtifactManifest
from jernerics.tracking.blob_uploader import upload_pending_blobs
from jernerics.tracking.infra import resolve_tracking_ship
from jernerics.tracking.stream_client import StreamClient


def _heartbeat_loop(
    path: Path, interval: float, stop: threading.Event, heartbeat
) -> None:
    while not stop.wait(interval):
        path.touch()
        heartbeat()


class TrialEnvironment:
    def __init__(
        self,
        *,
        tracking_dir: str,
        trial_number: int,
        server_addr: str | None = None,
        heartbeat_interval_s: float = 60.0,
        trial_id: TrialId | None = None,
    ) -> None:
        self._tracking_dir = tracking_dir
        self._trial_number = trial_number
        self._server_addr = server_addr
        self._heartbeat_interval_s = heartbeat_interval_s
        self._trial_id = trial_id

        self.trial_id: TrialId | None = None
        self.execution_id: ExecutionId | None = None
        self.tracker: Tracker | None = None
        self._sync_client: StreamClient | None = None
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._manifest_path: Path | None = None
        self._base_url: str | None = None
        self._api_key: str | None = None
        self._finished = False
        self._closed = False

    def start(self) -> Self:
        if not self._tracking_dir:
            return self

        tracking_dir = Path(self._tracking_dir)
        events_dir = tracking_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)

        from jernerics.tracking.tracker import JsonlTracker

        self.trial_id = self._trial_id if self._trial_id is not None else uuid4()
        self.execution_id = uuid4()
        events_path = events_dir / f"{self._trial_number}.jsonl"
        manifest_dir = tracking_dir / "artifacts"
        self._manifest_path = manifest_dir / f"{self._trial_number}.manifest"
        tracker = JsonlTracker(
            events_path,
            self.trial_id,
            self.execution_id,
            manifest=ArtifactManifest(self._manifest_path),
        )
        tracker.emit_execution_start()
        self.tracker = tracker

        if self._server_addr:
            ship = resolve_tracking_ship(self._server_addr)
            if ship:
                base_url, api_key = ship
                self._base_url = base_url
                self._api_key = api_key
                self._sync_client = StreamClient(
                    base_url=base_url,
                    path=events_path,
                    api_key=api_key,
                )
                self._sync_client.start()

        hb_dir = tracking_dir / "heartbeats"
        hb_dir.mkdir(parents=True, exist_ok=True)
        hb_path = hb_dir / f"{self._trial_number}.heartbeat"
        hb_path.touch()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            args=(
                hb_path,
                self._heartbeat_interval_s,
                self._heartbeat_stop,
                tracker.emit_heartbeat,
            ),
            daemon=True,
        )
        self._heartbeat_thread.start()

        return self

    def __enter__(self) -> Self:
        return self.start()

    def finish_execution(
        self,
        outcome: ExecutionOutcome,
        *,
        exit_code: int | None = None,
        failure_kind: FailureKind | None = None,
        failure_summary: str | None = None,
    ) -> ExecutionEndEvent | None:
        """Emit the single execution_end and stop heartbeats, files, shipping.

        The runner calls this only after the optimizer commit (or a factual
        failure), so the event's terminal facts are never premature. When no
        terminal evidence ever arrives, ``execution_end`` is simply absent and
        the execution stays incomplete server-side.
        """
        if self._finished:
            return None
        self._finished = True
        if failure_summary is not None:
            failure_summary = failure_summary[:2000]
        self._stop_heartbeats()
        end: ExecutionEndEvent | None = None
        if self.tracker is not None:
            end = self.tracker.emit_execution_end(
                outcome,
                exit_code=exit_code,
                failure_kind=failure_kind,
                failure_summary=failure_summary,
            )
        self.close()
        self._upload_blobs()
        return end

    def _upload_blobs(self) -> None:
        """Best-effort upload of this trial's declared blobs after the end.

        Declarations ship with the event log; blobs follow here once. Any
        failure only delays the upload — the post-hook sweeps every
        manifest again after the sweep batch completes.
        """
        if self._manifest_path is None or self._base_url is None:
            return
        try:
            result = upload_pending_blobs(
                self._base_url, self._api_key, [self._manifest_path]
            )
        except Exception as exc:
            print(f"jernerics: blob upload failed: {exc!r}", file=sys.stderr)
            return
        if result.failed:
            print(
                f"jernerics: {result.failed} blob upload(s) failed; "
                "the post-hook will retry",
                file=sys.stderr,
            )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_heartbeats()
        if self.tracker is not None:
            self.tracker.close()
        if self._sync_client is not None:
            self._sync_client.join()

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def _stop_heartbeats(self) -> None:
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5.0)
            self._heartbeat_thread = None
