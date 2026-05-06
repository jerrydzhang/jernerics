"""CLI entry point for replaying .pb files and syncing artifacts.

Invoked on HPC via SSH from the sync command.

Usage:
    python -m jernerics.tracking.replay_runner \
        --tracking-dir /cache/tracking --server-addr host:port
"""

import argparse
import os
import sys
from pathlib import Path

from jernerics.tracking.batch_sync import replay_tracking, sync_artifacts
from jernerics.tracking.infra import resolve_artifact_storage, resolve_streaming


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

    streaming = resolve_streaming(args.server_addr)
    if not streaming:
        print("Error: failed to connect to tracking server", file=sys.stderr)
        sys.exit(1)

    channel, stub = streaming
    api_key = os.environ.get("JERNERICS_API_KEY")
    metadata = [("x-api-key", api_key)] if api_key else None

    try:
        result = replay_tracking(
            tracking_dir=Path(args.tracking_dir),
            stub=stub,
            study=args.study,
            max_workers=args.max_workers,
            metadata=metadata,
        )
    finally:
        channel.close()

    if result.errors:
        sys.exit(1)

    # Step 2: Sync artifacts (graceful skip if env vars absent)
    upload_fn = resolve_artifact_storage()
    if upload_fn:
        print("Syncing artifacts...", file=sys.stderr)
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
