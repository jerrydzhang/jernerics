"""Runner-side trial environment: execution lifecycle, heartbeats, live shipping."""

import threading
from pathlib import Path
from typing import Self
from uuid import uuid4

from jernerics_schema import ExecutionId, ExecutionOutcome, TrialId

from jernerics.tracking import Tracker
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

    def __enter__(self) -> Self:
        if not self._tracking_dir:
            return self

        tracking_dir = Path(self._tracking_dir)
        events_dir = tracking_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)

        from jernerics.tracking.tracker import JsonlTracker

        self.trial_id = self._trial_id if self._trial_id is not None else uuid4()
        self.execution_id = uuid4()
        events_path = events_dir / f"{self._trial_number}.jsonl"
        tracker = JsonlTracker(events_path, self.trial_id, self.execution_id)
        tracker.emit_execution_start()
        self.tracker = tracker

        if self._server_addr:
            ship = resolve_tracking_ship(self._server_addr)
            if ship:
                base_url, api_key = ship
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

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._heartbeat_stop:
            self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5.0)
        if self.tracker:
            if exc_type is None:
                self.tracker.emit_execution_end(outcome=ExecutionOutcome.SUCCESS)
            else:
                self.tracker.emit_execution_end(
                    outcome=ExecutionOutcome.FAILURE,
                    failure_summary=(
                        repr(exc_val)[:2000] if exc_val is not None else None
                    ),
                )
            self.tracker.close()
        if self._sync_client:
            self._sync_client.join()
