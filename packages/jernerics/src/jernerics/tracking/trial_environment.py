import os
import threading
from pathlib import Path
from typing import Self

import grpc

from jernerics.tracking import Tracker
from jernerics.tracking.artifact_uploader import ArtifactUploader
from jernerics.tracking.infra import resolve_artifact_storage, resolve_streaming
from jernerics.tracking.stream_client import StreamClient


def _heartbeat_loop(path: Path, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        path.touch()


class TrialEnvironment:
    def __init__(
        self,
        *,
        tracking_dir: str,
        project_name: str,
        study_name: str,
        trial_number: int,
        server_addr: str | None = None,
        heartbeat_interval_s: float = 60.0,
    ) -> None:
        self._tracking_dir = tracking_dir
        self._project_name = project_name
        self._study_name = study_name
        self._trial_number = trial_number
        self._server_addr = server_addr
        self._heartbeat_interval_s = heartbeat_interval_s

        self._sync_client: StreamClient | None = None
        self._artifact_uploader: ArtifactUploader | None = None
        self._channel: grpc.Channel | None = None
        self._heartbeat_stop: threading.Event | None = None

        self.tracker: Tracker | None = None

    def __enter__(self) -> Self:
        if not self._tracking_dir:
            return self

        tracking_dir = Path(self._tracking_dir)

        events_dir = tracking_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)

        artifacts_dir = tracking_dir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = artifacts_dir / f"{self._trial_number}.manifest"
        cursor_path = artifacts_dir / f"{self._trial_number}.cursor"

        from jernerics.tracking.tracker import ProtobufTracker

        self.tracker = ProtobufTracker(
            self._project_name,
            self._study_name,
            self._trial_number,
            events_dir / f"{self._trial_number}.pb",
            manifest_path=manifest_path,
        )

        if self._server_addr:
            streaming = resolve_streaming(self._server_addr)
            if streaming:
                self._channel, stub = streaming
                self._sync_client = StreamClient(
                    stub,
                    events_dir / f"{self._trial_number}.pb",
                    api_key=os.environ.get("JERNERICS_API_KEY"),
                )
                self._sync_client.start()

        upload_fn = resolve_artifact_storage()
        if upload_fn:
            self._artifact_uploader = ArtifactUploader(
                manifest_path=manifest_path,
                cursor_path=cursor_path,
                upload_fn=upload_fn,
                project=self._project_name,
                study=self._study_name,
                trial_id=self._trial_number,
            )
            self._artifact_uploader.start()

        hb_dir = tracking_dir / "heartbeats"
        hb_dir.mkdir(parents=True, exist_ok=True)
        hb_path = hb_dir / f"{self._trial_number}.heartbeat"
        hb_path.touch()
        self._heartbeat_stop = threading.Event()
        threading.Thread(
            target=_heartbeat_loop,
            args=(hb_path, self._heartbeat_interval_s, self._heartbeat_stop),
            daemon=True,
        ).start()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._heartbeat_stop:
            self._heartbeat_stop.set()
        if self._artifact_uploader:
            self._artifact_uploader.join()
        if self.tracker:
            self.tracker.close()
        if self._sync_client:
            self._sync_client.join()
        if self._channel:
            self._channel.close()
