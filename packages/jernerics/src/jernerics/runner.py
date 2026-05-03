import os
import sys
import threading
from pathlib import Path
from typing import Any

import boto3
import grpc
import optuna
from jernerics_proto import tracking_pb2_grpc
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.config import load_config
from jernerics.dag import DAG
from jernerics.tracking import ProtobufTracker, Tracker
from jernerics.tracking.artifact_uploader import ArtifactUploader
from jernerics.tracking.grpc_channel import grpc_channel
from jernerics.tracking.stream_client import StreamClient


class _TaskFailure(Exception):
    pass


def _heartbeat_loop(path: Path, interval: float, stop: threading.Event) -> None:
    while not stop.wait(interval):
        path.touch()


def _make_s3_upload_fn(bucket: str) -> Any:
    s3 = boto3.client("s3")

    def upload_file(s3_key: str, local_path: str) -> None:
        s3.upload_file(local_path, bucket, s3_key)

    return upload_file


def run_trial(
    dag_file: str,
    config_file: str,
    study_name: str,
    storage_url: str,
    tracking_dir: str | None = None,
    project_name: str | None = None,
    server_addr: str | None = None,
    heartbeat_interval_s: float = 60.0,
) -> None:
    dag_dir = Path(dag_file).parent
    if str(dag_dir) not in sys.path:
        sys.path.insert(0, str(dag_dir))

    sweep = load_config(config_file)
    dag = DAG(dag_file, project_name)
    study = optuna.load_study(
        study_name=study_name,
        storage=JournalStorage(JournalFileBackend(storage_url)),
    )

    def objective(trial: optuna.trial.Trial) -> float:
        tracker: Tracker | None = None
        sync_client: StreamClient | None = None
        artifact_uploader: ArtifactUploader | None = None
        channel: grpc.Channel | None = None
        heartbeat_stop: threading.Event | None = None

        manifest_path: Path | None = None

        if tracking_dir:
            events_dir = Path(tracking_dir) / "events"
            events_dir.mkdir(parents=True, exist_ok=True)

            artifacts_dir = Path(tracking_dir) / "artifacts"
            artifacts_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = artifacts_dir / f"{trial.number}.manifest"
            cursor_path = artifacts_dir / f"{trial.number}.cursor"

            tracker = ProtobufTracker(
                project_name or "",
                study_name,
                trial.number,
                events_dir / f"{trial.number}.pb",
                manifest_path=manifest_path,
            )

        if server_addr and tracker:
            assert tracking_dir is not None
            channel = grpc_channel(server_addr)
            stub = tracking_pb2_grpc.TrackingServiceStub(channel)
            sync_client = StreamClient(
                stub,
                Path(tracking_dir) / "events" / f"{trial.number}.pb",
                api_key=os.environ.get("JERNERICS_API_KEY"),
            )
            sync_client.start()

        if tracking_dir and manifest_path:
            bucket = os.environ.get("JERNERICS_ARTIFACT_BUCKET")
            endpoint = os.environ.get("AWS_ENDPOINT_URL")
            if bucket and endpoint:
                artifact_uploader = ArtifactUploader(
                    manifest_path=manifest_path,
                    cursor_path=cursor_path,
                    upload_fn=_make_s3_upload_fn(bucket),
                    project=project_name or "",
                    study=study_name,
                    trial_id=trial.number,
                )
                artifact_uploader.start()

        if tracking_dir:
            hb_dir = Path(tracking_dir) / "heartbeats"
            hb_dir.mkdir(parents=True, exist_ok=True)
            hb_path = hb_dir / f"{trial.number}.heartbeat"
            hb_path.touch()
            heartbeat_stop = threading.Event()
            threading.Thread(
                target=_heartbeat_loop,
                args=(hb_path, heartbeat_interval_s, heartbeat_stop),
                daemon=True,
            ).start()

        params: dict[str, Any] = sweep.search_space(trial) if sweep.search_space else {}

        if params and set(sweep.base) & set(params):
            overlap = sorted(set(sweep.base) & set(params))
            raise ValueError(
                f"Config keys defined in both base and search_space: {overlap}. "
                "Please remove the overlapping keys from either 'base' or "
                "'search_space' in the config file."
            )

        config = {**sweep.base, **params, "config_index": trial.number}

        try:
            results = dag.run(
                config,
                tracker=tracker,
                runner=sweep.runner,
            )

            failed_tasks = [
                (name, res) for name, res in results.items() if res.is_error
            ]
            if failed_tasks:
                for task_name, task_result in failed_tasks:
                    print(
                        f"\t[{task_name}] Task failed with exception:",
                        file=sys.stderr,
                    )
                    print(task_result.error_traceback, file=sys.stderr)
                raise _TaskFailure

            print(f"Trial {trial.number + 1} completed", file=sys.stderr)

            if sweep.objective:
                return sweep.objective(results)
            return 0.0
        finally:
            if heartbeat_stop:
                heartbeat_stop.set()
            if artifact_uploader:
                artifact_uploader.join()
            if tracker:
                tracker.close()
            if sync_client:
                sync_client.join()
            if channel:
                channel.close()

    try:
        study.optimize(objective, n_trials=1)
    except _TaskFailure:
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("dag_file")
    parser.add_argument("config_file")
    parser.add_argument("--study-name")
    parser.add_argument("--storage-url")
    parser.add_argument("--tracking-dir")
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--server-addr", default=None)
    parser.add_argument("--heartbeat-interval", type=float, default=60.0)
    args = parser.parse_args()

    run_trial(
        dag_file=args.dag_file,
        config_file=args.config_file,
        study_name=args.study_name,
        storage_url=args.storage_url,
        tracking_dir=args.tracking_dir,
        project_name=args.project_name,
        server_addr=args.server_addr,
        heartbeat_interval_s=args.heartbeat_interval,
    )
