import threading
from pathlib import Path
from typing import Self

from jernerics.tracking import Tracker
from jernerics.tracking.artifact_uploader import ArtifactUploader
from jernerics.tracking.infra import resolve_artifact_storage, resolve_tracking_ship
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

        from jernerics.tracking.tracker import JsonlTracker

        events_path = events_dir / f"{self._trial_number}.jsonl"
        self.tracker = JsonlTracker(
            self._project_name,
            self._study_name,
            self._trial_number,
            events_path,
            manifest_path=manifest_path,
        )

        ship = resolve_tracking_ship(self._server_addr) if self._server_addr else None
        if ship:
            base_url, api_key = ship
            self._sync_client = StreamClient(
                base_url=base_url,
                path=events_path,
                api_key=api_key,
            )
            self._sync_client.start()
        else:
            base_url = None

        upload_fn = resolve_artifact_storage(base_url)
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
