"""CLI entry point for replaying JSONL files and syncing artifacts.

Invoked on HPC via SSH from the sync command.

Usage:
    python -m jernerics.tracking.replay_runner \
        --tracking-dir /cache/tracking --server-addr http://host:port
"""

import argparse
import os
import sys
from pathlib import Path

from jernerics.tracking.batch_sync import replay_tracking, sync_artifacts
from jernerics.tracking.infra import resolve_artifact_storage, resolve_tracking_ship


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--tracking-dir", required=True, help="Path to tracking directory"
    )
    parser.add_argument(
        "--server-addr",
        required=True,
        help="Tracking server base URL (http://host:port)",
    )
    parser.add_argument("--study", default=None, help="Scope to a single study")
    parser.add_argument("--max-workers", type=int, default=16, help="Thread pool size")
    args = parser.parse_args()

    ship = resolve_tracking_ship(args.server_addr)
    if not ship:
        print("Error: no tracking server configured", file=sys.stderr)
        sys.exit(1)

    base_url, api_key = ship

    result = replay_tracking(
        tracking_dir=Path(args.tracking_dir),
        base_url=base_url,
        api_key=api_key,
        study=args.study,
        max_workers=args.max_workers,
    )

    if result.errors:
        sys.exit(1)

    upload_fn = resolve_artifact_storage(base_url)
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
        print("Artifact sync skipped (no storage configured).", file=sys.stderr)


if __name__ == "__main__":
    main()
