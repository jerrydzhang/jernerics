"""Post-hook pipeline: retry → optuna sync → artifact sync.

Invoked via ``python -m jernerics.post_hook`` after each sweep batch.
"""

import argparse
import enum
import os
import sys
from pathlib import Path

from jernerics.retry import RetryContext
from jernerics.retry_checker import run_checker
from jernerics.tracking.data_sync import replay_tracking, sync_artifacts


class PipelineResult(enum.Enum):
    SWEEP_COMPLETE = "sweep_complete"
    RETRY_SUBMITTED = "retry_submitted"


def _env(name: str) -> str | None:
    return os.environ.get(name) or None


def _make_s3_upload_fn(bucket: str):
    import boto3

    s3 = boto3.client("s3")

    def upload_file(s3_key: str, local_path: str) -> None:
        s3.upload_file(local_path, bucket, s3_key)

    return upload_file


def run_pipeline(
    ctx_path: str,
    chain_depth: int,
    tracking_dir: str,
    storage_path: str,
    *,
    upload_fn=None,
    stub=None,
) -> PipelineResult:
    submitted = run_checker(ctx_path=ctx_path, chain_depth=chain_depth)

    if submitted:
        return PipelineResult.RETRY_SUBMITTED

    if upload_fn is not None or stub is not None:
        ctx = RetryContext.from_json(Path(ctx_path).read_text())

    if upload_fn is not None:
        s3_key = f"{ctx.project_name}/{ctx.study_name}/optuna.journal"
        upload_fn(s3_key, storage_path)

    if stub is not None:
        tracking_dir_path = Path(tracking_dir)
        replay_tracking(
            tracking_dir=tracking_dir_path.parent,
            stub=stub,
            study=ctx.study_name,
        )
        sync_artifacts(
            tracking_dir=tracking_dir_path.parent,
            upload_fn=upload_fn,
            project=ctx.project_name or "",
            study=ctx.study_name,
        )

    return PipelineResult.SWEEP_COMPLETE


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    parser.add_argument("--tracking-dir", required=True)
    parser.add_argument("--storage-path", required=True)
    parser.add_argument("--server-addr", default=None)
    args = parser.parse_args()

    upload_fn = None
    bucket = _env("JERNERICS_ARTIFACT_BUCKET")
    endpoint = _env("AWS_ENDPOINT_URL")
    if bucket and endpoint:
        upload_fn = _make_s3_upload_fn(bucket)

    stub = None
    server_addr = args.server_addr
    if server_addr:
        from jernerics_proto import tracking_pb2_grpc

        from jernerics.tracking.grpc_channel import grpc_channel

        channel = grpc_channel(server_addr)
        stub = tracking_pb2_grpc.TrackingServiceStub(channel)

    result = run_pipeline(
        ctx_path=args.context,
        chain_depth=args.chain_depth,
        tracking_dir=args.tracking_dir,
        storage_path=args.storage_path,
        upload_fn=upload_fn,
        stub=stub,
    )

    if result == PipelineResult.RETRY_SUBMITTED:
        sys.exit(0)
