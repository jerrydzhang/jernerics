import itertools
import traceback
from pathlib import Path

import optuna
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
from jernerics.tracking.batch_sync import replay_tracking
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

        self._emit_submission_events(spec, tracker_dir)

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

        if any_failed:
            raise RuntimeError("One or more trials failed")

        # Post-hook pipeline: sync tracking events to the server
        if self.tracking_server:
            tracking_parent = tracker_dir.parent
            self._run_post_hook(tracking_parent, spec)

        return SubmitResult(
            submissions=[JobSubmission(job_id="local", n_trials=spec.n_trials)]
        )

    def _run_post_hook(self, tracking_dir, spec: SweepSubmission) -> None:
        ship = resolve_tracking_ship(self.tracking_server or "")
        if not ship:
            return

        base_url, api_key = ship

        replay_tracking(
            tracking_dir=tracking_dir,
            base_url=base_url,
            api_key=api_key,
            study=spec.study_name,
        )

    def _emit_submission_events(self, spec: SweepSubmission, tracker_dir: Path) -> None:
        if not spec.project_name:
            return
        result = SubmitResult(
            submissions=[JobSubmission(job_id="local", n_trials=spec.n_trials)]
        )
        events = build_submission_events(spec, "local", result)
        path = tracker_dir / "submission" / f"{spec.submission_id}.jsonl"
        with TrackingWriter(path) as writer:
            for event in events:
                writer.write_event(event)
