"""Post-hook pipeline: retry → optuna sync → artifact sync.

Invoked via ``python -m jernerics.post_hook`` after each sweep batch.
"""

import argparse
import enum
import sys

from jernerics.retry_checker import run_checker


class PipelineResult(enum.Enum):
    SWEEP_COMPLETE = "sweep_complete"
    RETRY_SUBMITTED = "retry_submitted"


def run_pipeline(
    ctx_path: str,
    chain_depth: int,
    tracking_dir: str,
    storage_path: str,
) -> PipelineResult:
    submitted = run_checker(ctx_path=ctx_path, chain_depth=chain_depth)

    if submitted:
        return PipelineResult.RETRY_SUBMITTED

    # TODO: optuna DB upload to minIO
    # TODO: artifact/tracking sync

    return PipelineResult.SWEEP_COMPLETE


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    parser.add_argument("--tracking-dir", required=True)
    parser.add_argument("--storage-path", required=True)
    args = parser.parse_args()

    result = run_pipeline(
        ctx_path=args.context,
        chain_depth=args.chain_depth,
        tracking_dir=args.tracking_dir,
        storage_path=args.storage_path,
    )

    if result == PipelineResult.RETRY_SUBMITTED:
        sys.exit(0)
