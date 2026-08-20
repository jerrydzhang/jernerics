"""Post-hook pipeline: retry detection → tracking replay.

Invoked via ``python -m jernerics.post_hook`` after each sweep batch.
"""

import argparse
import enum
import sys
from pathlib import Path

from jernerics.retry import RetryContext
from jernerics.retry_checker import run_checker
from jernerics.tracking.batch_sync import replay_tracking
from jernerics.tracking.infra import resolve_tracking_ship


class PipelineResult(enum.Enum):
    SWEEP_COMPLETE = "sweep_complete"
    RETRY_SUBMITTED = "retry_submitted"


def run_pipeline(
    ctx_path: str,
    chain_depth: int,
    tracking_dir: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> PipelineResult:
    submitted = run_checker(ctx_path=ctx_path, chain_depth=chain_depth)

    if submitted:
        return PipelineResult.RETRY_SUBMITTED

    if base_url is not None:
        ctx = RetryContext.from_json(Path(ctx_path).read_text())
        replay_tracking(
            tracking_dir=Path(tracking_dir).parent,
            base_url=base_url,
            api_key=api_key,
            study=ctx.study_name,
        )

    return PipelineResult.SWEEP_COMPLETE


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    parser.add_argument("--tracking-dir", required=True)
    parser.add_argument("--server-addr", default=None)
    args = parser.parse_args()

    base_url = None
    api_key = None
    if args.server_addr:
        ship = resolve_tracking_ship(args.server_addr)
        if ship:
            base_url, api_key = ship

    result = run_pipeline(
        ctx_path=args.context,
        chain_depth=args.chain_depth,
        tracking_dir=args.tracking_dir,
        base_url=base_url,
        api_key=api_key,
    )

    if result == PipelineResult.RETRY_SUBMITTED:
        sys.exit(0)
