"""Post-hook pipeline: retry → optuna sync → artifact sync.

Invoked via ``python -m jernerics.post_hook`` after each sweep batch.
"""

import argparse
import enum
import sys
from pathlib import Path

from jernerics.retry import RetryContext
from jernerics.retry_checker import run_checker
from jernerics.tracking.batch_sync import replay_tracking, sync_artifacts
from jernerics.tracking.infra import resolve_artifact_storage, resolve_tracking_ship


class PipelineResult(enum.Enum):
    SWEEP_COMPLETE = "sweep_complete"
    RETRY_SUBMITTED = "retry_submitted"


def run_pipeline(
    ctx_path: str,
    chain_depth: int,
    tracking_dir: str,
    storage_path: str,
    *,
    upload_fn=None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> PipelineResult:
    submitted = run_checker(ctx_path=ctx_path, chain_depth=chain_depth)

    if submitted:
        return PipelineResult.RETRY_SUBMITTED

    if upload_fn is not None or base_url is not None:
        ctx = RetryContext.from_json(Path(ctx_path).read_text())

    if upload_fn is not None:
        artifact_key = f"{ctx.project_name}/{ctx.study_name}/optuna.journal"
        upload_fn(artifact_key, storage_path)

    if base_url is not None:
        tracking_dir_path = Path(tracking_dir)
        replay_tracking(
            tracking_dir=tracking_dir_path.parent,
            base_url=base_url,
            api_key=api_key,
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

    base_url = None
    api_key = None
    if args.server_addr:
        ship = resolve_tracking_ship(args.server_addr)
        if ship:
            base_url, api_key = ship

    upload_fn = resolve_artifact_storage(base_url)

    result = run_pipeline(
        ctx_path=args.context,
        chain_depth=args.chain_depth,
        tracking_dir=args.tracking_dir,
        storage_path=args.storage_path,
        upload_fn=upload_fn,
        base_url=base_url,
        api_key=api_key,
    )

    if result == PipelineResult.RETRY_SUBMITTED:
        sys.exit(0)
