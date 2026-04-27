"""CLI entry point for replaying .pb files to the tracking server.

Invoked on HPC via SSH from the sync command.

Usage:
    python -m jernerics.tracking.replay_runner \
        --tracking-dir /cache/tracking --server-addr host:port
"""

import argparse
import sys

import grpc
from jernerics_proto import tracking_pb2_grpc

from jernerics.tracking.data_sync import replay_tracking


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

    channel = grpc.insecure_channel(args.server_addr)
    stub = tracking_pb2_grpc.TrackingServiceStub(channel)

    try:
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


if __name__ == "__main__":
    main()
