import itertools
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path

import optuna
from jernerics_schema import SweepSnapshotEvent, sweep_id_for
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.backend.models import (
    JobSubmission,
    SubmitResult,
    SweepSubmission,
)
from jernerics.backend.submission import build_submission_events
from jernerics.config import load_config
from jernerics.paths import cache_dir
from jernerics.runner import run_trial
from jernerics.tracking.batch_sync import replay_tracking, ship_events_file
from jernerics.tracking.blob_uploader import sweep_manifest_blobs
from jernerics.tracking.infra import resolve_tracking_ship
from jernerics.tracking.jsonl_io import TrackingWriter


class LocalBackend:
    def __init__(self, tracking_server: str | None = None):
        self.tracking_server = tracking_server

    def storage_path(self, study_name: str) -> str:
        project_cache = cache_dir()
        return str(project_cache / "optuna" / f"{study_name}.journal")

    def submit_sweep(
        self, spec: SweepSubmission, *, direction: str = "minimize"
    ) -> SubmitResult:
        project_cache = cache_dir()
        tracker_dir = (
            Path(spec.tracking_dir)
            if spec.tracking_dir
            else (project_cache / "tracking" / spec.study_name)
        )
        tracker_dir.mkdir(parents=True, exist_ok=True)

        storage = JournalStorage(JournalFileBackend(spec.storage_url))
        sweep = load_config(str(spec.config_path))
        study = optuna.create_study(
            study_name=spec.study_name,
            storage=storage,
            direction=direction,
            sampler=sweep.sampler,
            load_if_exists=True,
        )

        if spec.grid:
            keys = sorted(spec.grid.keys())
            for combo in itertools.product(*[spec.grid[k] for k in keys]):
                study.enqueue_trial(dict(zip(keys, combo, strict=True)))

        submission_events_path = self._emit_submission_events(spec, tracker_dir)
        self._ship_submission_events(spec, submission_events_path)

        any_failed = False

        for i in range(spec.n_trials):
            print(f"Running trial {i + 1}/{spec.n_trials}", flush=True)

            try:
                run_trial(
                    trial_file=str(spec.trial_path),
                    config_file=str(spec.config_path),
                    study_name=spec.study_name,
                    storage_url=spec.storage_url,
                    tracking_dir=str(tracker_dir),
                    project_name=spec.project_name,
                    server_addr=spec.server_addr or self.tracking_server,
                )
            except SystemExit as e:
                if e.code != 0:
                    any_failed = True
            except Exception:
                traceback.print_exc()
                any_failed = True

        terminal_events_path = self._emit_terminal_sweep_event(
            spec, submission_events_path, failed=any_failed
        )
        self._ship_submission_events(spec, terminal_events_path)

        # Post-hook pipeline: blob retry + tracking event replay
        if self.tracking_server:
            self._run_post_hook(tracker_dir, spec)

        if any_failed:
            raise RuntimeError("One or more trials failed")

        return SubmitResult(
            submissions=[JobSubmission(job_id="local", n_trials=spec.n_trials)]
        )

    def _run_post_hook(self, tracking_dir, spec: SweepSubmission) -> None:
        ship = resolve_tracking_ship(self.tracking_server or "")
        if not ship:
            return

        base_url, api_key = ship

        sweep_manifest_blobs(tracking_dir, base_url, api_key)
        replay_tracking(
            tracking_dir=Path(tracking_dir).parent,
            base_url=base_url,
            api_key=api_key,
            study=spec.study_name,
        )

    def _ship_submission_events(self, spec: SweepSubmission, path: Path | None) -> None:
        """Land the submission events before trials stream live.

        Ingest validates every trial event against a known sweep, so
        the sweep snapshot must be on the server when the first trial
        ships. Best-effort: the post-hook replay stays the delivery
        guarantee.
        """
        if path is None:
            return
        ship = resolve_tracking_ship(spec.server_addr or self.tracking_server or "")
        if not ship:
            return
        base_url, api_key = ship
        ship_events_file(path, base_url, api_key)

    def _emit_submission_events(
        self, spec: SweepSubmission, tracker_dir: Path
    ) -> Path | None:
        if not spec.project_name:
            return None
        result = SubmitResult(
            submissions=[JobSubmission(job_id="local", n_trials=spec.n_trials)]
        )
        events = build_submission_events(spec, "local", result)
        path = tracker_dir / "submission" / f"{spec.submission_id}.jsonl"
        with TrackingWriter(path) as writer:
            for event in events:
                writer.write_event(event)
        return path

    def _emit_terminal_sweep_event(
        self, spec: SweepSubmission, path: Path | None, *, failed: bool
    ) -> Path | None:
        """Append the sweep's terminal snapshot to its submission events."""
        if path is None:
            return None
        event = SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=datetime.now(UTC),
            project=spec.project_name or "",
            sweep_id=sweep_id_for(spec.project_name or "", spec.study_name),
            name=spec.study_name,
            state="failed" if failed else "completed",
        )
        with TrackingWriter(path) as writer:
            writer.write_event(event)
        return path
