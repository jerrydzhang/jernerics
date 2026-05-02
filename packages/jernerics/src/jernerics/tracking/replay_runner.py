"""CLI entry point for replaying .pb files and syncing artifacts.

Invoked on HPC via SSH from the sync command.

Usage:
    python -m jernerics.tracking.replay_runner \
        --tracking-dir /cache/tracking --server-addr host:port
"""

import argparse
import os
import sys

from jernerics_proto import tracking_pb2_grpc

from jernerics.tracking.data_sync import replay_tracking, sync_artifacts
from jernerics.tracking.grpc_channel import grpc_channel


def _make_s3_upload_fn(bucket: str):
    import boto3  # ty: ignore[unresolved-import]

    s3 = boto3.client("s3")

    def upload_file(s3_key: str, local_path: str) -> None:
        s3.upload_file(local_path, bucket, s3_key)

    return upload_file


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tracking-dir", required=True, help="Path to tracking directory"
    )
    parser.add_argument(
        "--server-addr", required=True, help="Tracking server address (host:port)"
    )
    parser.add_argument("--study", default=None, help="Scope to a single study")
    parser.add_argument("--max-workers", type=int, default=16, help="Thread pool size")
    args = parser.parse_args()

    channel = grpc_channel(args.server_addr)
    stub = tracking_pb2_grpc.TrackingServiceStub(channel)

    try:
        # Step 1: Replay tracking events
        result = replay_tracking(
            tracking_dir=args.tracking_dir,
            stub=stub,
            study=args.study,
            max_workers=args.max_workers,
        )
    finally:
        channel.close()

    if result.errors:
        sys.exit(1)

    # Step 2: Sync artifacts (graceful skip if env vars absent)
    bucket = os.environ.get("JERNERICS_ARTIFACT_BUCKET")
    endpoint = os.environ.get("AWS_ENDPOINT_URL")

    if bucket and endpoint:
        print("Syncing artifacts...", file=sys.stderr)
        upload_fn = _make_s3_upload_fn(bucket)
        from pathlib import Path

        sync_artifacts(
            Path(args.tracking_dir),
            upload_fn=upload_fn,
            project=os.environ.get("JERNERICS_PROJECT", ""),
            study=args.study or "",
        )
        print("Artifact sync complete.", file=sys.stderr)
    else:
        print("Artifact sync skipped (no S3 env vars).", file=sys.stderr)


if __name__ == "__main__":
    main()
