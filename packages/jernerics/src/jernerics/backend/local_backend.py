import itertools
import os
from pathlib import Path

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.backend.models import (
    JobSubmission,
    SubmitResult,
    SweepSubmission,
)
from jernerics.config import load_config
from jernerics.paths import cache_dir
from jernerics.runner import run_trial
from jernerics.tracking.batch_sync import replay_tracking, sync_artifacts
from jernerics.tracking.infra import resolve_artifact_storage, resolve_streaming


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

        any_failed = False

        for i in range(spec.n_trials):
            print(f"Running trial {i + 1}/{spec.n_trials}", flush=True)

            try:
                run_trial(
                    dag_file=str(spec.dag_path),
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
                any_failed = True

        if any_failed:
            raise RuntimeError("One or more trials failed")

        # Post-hook pipeline: sync tracking and artifacts
        if self.tracking_server:
            tracking_parent = tracker_dir.parent
            self._run_post_hook(tracking_parent, spec)

        return SubmitResult(
            submissions=[JobSubmission(job_id="local", n_trials=spec.n_trials)]
        )

    def _run_post_hook(self, tracking_dir, spec: SweepSubmission) -> None:
        streaming = resolve_streaming(self.tracking_server or "")
        if not streaming:
            return

        channel, stub = streaming

        api_key = os.environ.get("JERNERICS_API_KEY")
        metadata = [("x-api-key", api_key)] if api_key else None

        replay_tracking(
            tracking_dir=tracking_dir,
            stub=stub,
            study=spec.study_name,
            metadata=metadata,
        )

        upload_fn = resolve_artifact_storage()
        if upload_fn:
            sync_artifacts(
                tracking_dir=tracking_dir,
                upload_fn=upload_fn,
                project=spec.project_name or "",
                study=spec.study_name,
            )
        channel.close()
