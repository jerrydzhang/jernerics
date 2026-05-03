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
from jernerics.config import ARTIFACT_ENV_VARS, load_config
from jernerics.paths import cache_dir
from jernerics.runner import run_trial
from jernerics.tracking.batch_sync import replay_tracking, sync_artifacts


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
        from jernerics_proto import tracking_pb2_grpc

        from jernerics.tracking.grpc_channel import grpc_channel

        channel = grpc_channel(self.tracking_server or "")
        stub = tracking_pb2_grpc.TrackingServiceStub(channel)

        api_key = os.environ.get("JERNERICS_API_KEY")
        metadata = [("x-api-key", api_key)] if api_key else None

        replay_tracking(
            tracking_dir=tracking_dir,
            stub=stub,
            study=spec.study_name,
            metadata=metadata,
        )

        artifact_env = {k: v for k in ARTIFACT_ENV_VARS if (v := os.environ.get(k))}
        if artifact_env.get("AWS_ENDPOINT_URL") and artifact_env.get(
            "JERNERICS_ARTIFACT_BUCKET"
        ):
            import boto3

            s3 = boto3.client("s3")
            bucket = artifact_env["JERNERICS_ARTIFACT_BUCKET"]

            def upload_fn(s3_key: str, local_path: str) -> None:
                s3.upload_file(local_path, bucket, s3_key)

            sync_artifacts(
                tracking_dir=tracking_dir,
                upload_fn=upload_fn,
                project=spec.project_name or "",
                study=spec.study_name,
            )
