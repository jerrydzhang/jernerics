"""Post-hook pipeline: retry detection → journal reconciliation → tracking replay.

Invoked via ``python -m jernerics.post_hook`` after each sweep batch.
"""

import argparse
import enum
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import optuna
from jernerics_schema import (
    JERNERICS_NAMESPACE,
    ConflictRecord,
    SweepSnapshotEvent,
    sweep_id_for,
)
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.optuna_mirror import frozen_trial_snapshot
from jernerics.retry import RetryContext
from jernerics.retry_checker import run_checker
from jernerics.tracking.batch_sync import replay_tracking
from jernerics.tracking.infra import resolve_tracking_ship


class PipelineResult(enum.Enum):
    SWEEP_COMPLETE = "sweep_complete"
    RETRY_SUBMITTED = "retry_submitted"


class ReconciliationConflictError(Exception):
    """The journal disagrees with terminal server state; nothing was overwritten."""

    def __init__(self, conflicts: list[ConflictRecord]) -> None:
        self.conflicts = list(conflicts)
        super().__init__(f"{len(self.conflicts)} terminal-state conflict(s)")


def reconcile_study(ctx: RetryContext, tracking_dir: str | Path) -> Path | None:
    """Snapshot every journal trial into the study's submission events dir.

    Two files ship in the same replay: ``reconcile-sweep.jsonl`` carries the
    sweep snapshot (so trial event logs never reference an unknown sweep),
    and ``reconcile.jsonl`` carries one snapshot per FrozenTrial. Snapshot
    identities and timestamps derive deterministically from the journal, so
    repeated reconciliations emit byte-identical events and re-reconcile as
    duplicates. A trial without a recorded live identity falls back to a
    deterministic uuid5 of sweep id + trial number.
    """
    if not ctx.storage_path or not Path(ctx.storage_path).exists():
        return None
    study = optuna.load_study(
        study_name=ctx.study_name,
        storage=JournalStorage(JournalFileBackend(ctx.storage_path)),
    )
    sweep_id = sweep_id_for(ctx.project_name or "", ctx.study_name)
    sweep_event = SweepSnapshotEvent(
        # The epoch timestamp and uuid5 ids keep re-reconciles duplicates.
        event_id=uuid.uuid5(JERNERICS_NAMESPACE, f"reconcile-sweep:{sweep_id}"),
        recorded_at=datetime(1970, 1, 1, tzinfo=UTC),
        project=ctx.project_name or "",
        sweep_id=sweep_id,
        name=ctx.study_name,
        state="running",
    )
    trial_events = [
        frozen_trial_snapshot(trial, sweep_id=sweep_id) for trial in study.trials
    ]
    submission_dir = Path(tracking_dir) / "submission"
    submission_dir.mkdir(parents=True, exist_ok=True)
    sweep_path = submission_dir / "reconcile-sweep.jsonl"
    sweep_path.write_text(sweep_event.model_dump_json() + "\n")
    path = submission_dir / "reconcile.jsonl"
    path.write_text("".join(event.model_dump_json() + "\n" for event in trial_events))
    return path


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
        reconcile_path = reconcile_study(ctx, tracking_dir)
        # Live trial event logs ship first (a running snapshot must land
        # before its terminal reconciliation); the reconcile snapshots ship
        # last so they close out or conflict with what already landed.
        conflicts: list[ConflictRecord] = []
        skips: list[set[Path] | None] = (
            [{reconcile_path}, None] if reconcile_path is not None else [None]
        )
        for skip in skips:
            result = replay_tracking(
                tracking_dir=Path(tracking_dir).parent,
                base_url=base_url,
                api_key=api_key,
                study=ctx.study_name,
                skip=skip,
            )
            conflicts.extend(result.conflicts)
        if conflicts:
            raise ReconciliationConflictError(conflicts)

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

    try:
        result = run_pipeline(
            ctx_path=args.context,
            chain_depth=args.chain_depth,
            tracking_dir=args.tracking_dir,
            base_url=base_url,
            api_key=api_key,
        )
    except ReconciliationConflictError as exc:
        for conflict in exc.conflicts:
            print(
                f"reconciliation conflict: trial {conflict.trial_id} "
                f"[{conflict.kind}] {conflict.detail}",
                file=sys.stderr,
            )
        sys.exit(1)

    if result == PipelineResult.RETRY_SUBMITTED:
        sys.exit(0)
