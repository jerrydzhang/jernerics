"""Post-hook pipeline: retry detection → journal reconciliation → tracking replay.

Invoked via ``python -m jernerics.post_hook`` after each sweep batch.
"""

import argparse
import enum
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import optuna
from jernerics_schema import (
    JERNERICS_NAMESPACE,
    ConflictRecord,
    ExecutionEndEvent,
    ExecutionId,
    ExecutionOutcome,
    ExecutionStartEvent,
    FailureKind,
    JobSnapshotEvent,
    SweepSnapshotEvent,
    TrialId,
    sweep_id_for,
)
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import FrozenTrial, TrialState

from jernerics.backend.job_meta import load_job_studies
from jernerics.backend.slurm.sacct import (
    build_job_resource_event,
    fetch_job_resources,
)
from jernerics.optuna_mirror import (
    _journal_timestamp,
    frozen_trial_snapshot,
    tracked_trial_id,
)
from jernerics.retry import RetryContext
from jernerics.retry_checker import run_checker
from jernerics.tracking.batch_sync import replay_tracking, ship_events_file
from jernerics.tracking.blob_uploader import sweep_manifest_blobs
from jernerics.tracking.infra import (
    TrackingServerSchemeError,
    resolve_tracking_ship,
)
from jernerics.tracking.jsonl_io import scan_events


class PipelineResult(enum.Enum):
    SWEEP_COMPLETE = "sweep_complete"
    RETRY_SUBMITTED = "retry_submitted"


class ReconciliationConflictError(Exception):
    """The journal disagrees with terminal server state; nothing was overwritten."""

    def __init__(self, conflicts: list[ConflictRecord]) -> None:
        self.conflicts = list(conflicts)
        super().__init__(f"{len(self.conflicts)} terminal-state conflict(s)")


def _local_executions(
    tracking_dir: Path,
) -> tuple[dict[ExecutionId, TrialId], set[ExecutionId]]:
    """Started executions (id -> owning trial) and ended ids in live logs."""
    started: dict[ExecutionId, TrialId] = {}
    ended: set[ExecutionId] = set()
    events_dir = tracking_dir / "events"
    if not events_dir.is_dir():
        return started, ended
    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            events, _ = scan_events(path, 0)
        except (OSError, ValueError):
            continue
        for event, _ in events:
            if isinstance(event, ExecutionStartEvent):
                started.setdefault(event.execution_id, event.trial_id)
            elif isinstance(event, ExecutionEndEvent):
                ended.add(event.execution_id)
    return started, ended


@dataclass(frozen=True)
class DeadExecution:
    execution_id: ExecutionId
    trial: FrozenTrial


_TERMINAL_OPTUNA_STATES = frozenset(
    {TrialState.COMPLETE, TrialState.FAIL, TrialState.PRUNED}
)

_DEAD_EXECUTION_SUMMARY = (
    "reconciled: execution outlived terminal trial; heartbeat stale"
)


def find_dead_executions(
    study: optuna.Study, *, sweep_id: uuid.UUID, tracking_dir: str | Path
) -> list[DeadExecution]:
    """Executions started locally but never ended whose trial is terminal.

    Journal terminality is the only deadness proof: a RUNNING trial with a
    quiet heartbeat may still resume, so it is never reconciled here.
    """
    started, ended = _local_executions(Path(tracking_dir))
    dead: list[DeadExecution] = []
    for trial in study.trials:
        if trial.state not in _TERMINAL_OPTUNA_STATES:
            continue
        trial_id = tracked_trial_id(dict(trial.user_attrs), sweep_id, trial.number)
        dead.extend(
            DeadExecution(execution_id=execution_id, trial=trial)
            for execution_id, owner in sorted(started.items())
            if owner == trial_id and execution_id not in ended
        )
    return dead


def _reconciled_execution_end(dead: DeadExecution) -> ExecutionEndEvent:
    stamp = _journal_timestamp(dead.trial)
    return ExecutionEndEvent(
        event_id=uuid.uuid5(
            JERNERICS_NAMESPACE, f"reconcile-end:{dead.execution_id}"
        ),
        recorded_at=stamp,
        execution_id=dead.execution_id,
        ended_at=stamp,
        outcome=ExecutionOutcome.FAILURE,
        exit_code=None,
        failure_kind=FailureKind.STALE_HEARTBEAT,
        failure_summary=_DEAD_EXECUTION_SUMMARY,
    )


def reconcile_study(ctx: RetryContext, tracking_dir: str | Path) -> list[Path]:
    """Snapshot journal trials and dead executions into submission files.

    Three files ship in the same replay: ``reconcile-sweep.jsonl`` carries
    the sweep snapshot (so trial event logs never reference an unknown
    sweep), ``reconcile.jsonl`` carries one snapshot per FrozenTrial, and
    ``reconcile-executions.jsonl`` carries one terminal end per execution
    that outlived its terminal trial. Identities and timestamps derive
    deterministically from the journal, so repeated reconciliations emit
    byte-identical events and re-reconcile as duplicates. A trial without
    a recorded live identity falls back to a deterministic uuid5 of sweep
    id + trial number. Returns the files that must ship after live event
    logs; the executions file is only written when it has content so an
    already-shipped end is never truncated away.
    """
    if not ctx.storage_path or not Path(ctx.storage_path).exists():
        return []
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
    written = [path]
    end_events = [
        _reconciled_execution_end(dead)
        for dead in find_dead_executions(
            study, sweep_id=sweep_id, tracking_dir=tracking_dir
        )
    ]
    if end_events:
        executions_path = submission_dir / "reconcile-executions.jsonl"
        executions_path.write_text(
            "".join(event.model_dump_json() + "\n" for event in end_events)
        )
        written.append(executions_path)
    return written


def _scheduler_task_log_files(cache_dir: Path) -> list[Path]:
    """Scheduler-written per-task log files derivable from job metadata."""
    jobs_dir = cache_dir / "jobs"
    if not jobs_dir.is_dir():
        return []
    found: list[Path] = []
    for meta_file in sorted(jobs_dir.glob("*.json")):
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        job_id = str(meta.get("job_id") or meta_file.stem)
        remote_dir = str(meta.get("remote_dir") or cache_dir)
        for pattern in (meta.get("output_pattern"), meta.get("error_pattern")):
            if not pattern:
                continue
            expanded = pattern.replace("%A", job_id).replace("%j", job_id)
            if "%a" in expanded:
                expanded = expanded.replace("%a", "*")
            candidate = Path(expanded).expanduser()
            if not candidate.is_absolute():
                candidate = Path(remote_dir).expanduser() / candidate
            if "*" in candidate.name:
                found.extend(sorted(candidate.parent.glob(candidate.name)))
            elif candidate.is_file():
                found.append(candidate)
    return sorted(set(found))


def _report_scheduler_task_logs(cache_dir: Path) -> None:
    """Map scheduler task logs to executions — or say why that is not possible.

    Job metadata records where the scheduler wrote per-task logs, but no
    trial or execution identity for any task: every backend launches the
    same generic runner command per task and trial numbers are drawn from
    the optimizer journal at runtime. Nothing in the metadata (or the
    events) ties a scheduler task index to a trial, so the logs are left
    in place with a note instead of an invented association. Execution
    stdout/stderr still arrive through the runner-declared system
    artifacts.
    """
    logs = _scheduler_task_log_files(cache_dir)
    if not logs:
        return
    print(
        f"jernerics: {len(logs)} scheduler task log file(s) found under "
        f"{cache_dir}, but job metadata records no trial association for "
        "scheduler tasks; leaving them in place rather than inventing one. "
        "Execution stdout/stderr are tracked via runner-declared system "
        "artifacts.",
        file=sys.stderr,
    )


def _sweep_job_ids(tracking_dir: Path, study_name: str) -> dict[str, str | None]:
    """Scheduler job ids of one study: submission events, else job metadata."""
    jobs: dict[str, str | None] = {}
    submission_dir = tracking_dir / "submission"
    if submission_dir.is_dir():
        for path in sorted(submission_dir.glob("*.jsonl")):
            try:
                events, _ = scan_events(path, 0)
            except (OSError, ValueError):
                continue
            for event, _ in events:
                if isinstance(event, JobSnapshotEvent):
                    jobs.setdefault(event.scheduler_job_id, str(event.submission_id))
    if jobs:
        return jobs
    studies = load_job_studies(tracking_dir.parent.parent)
    return {job_id: None for job_id, study in studies.items() if study == study_name}


def capture_job_resources(
    tracking_dir: str, study_name: str, base_url: str, api_key: str | None
) -> None:
    """Best-effort sacct capture for the study's jobs; never raises.

    Assumes sacct is reachable from where the post-hook runs; a cluster
    that blocks compute-node slurmdbd access makes every fetch fail with
    one stderr line each — the ``jernerics job resources`` backfill CLI
    is the recovery path there (verify on first real deployment).
    """
    try:
        job_ids = _sweep_job_ids(Path(tracking_dir), study_name)
        if not job_ids:
            print(
                f"jernerics: no job ids found for study {study_name}; "
                "resource capture skipped.",
                file=sys.stderr,
            )
            return
        events = []
        for job_id, submission_id in sorted(job_ids.items()):
            result = fetch_job_resources(job_id)
            if result.error is not None or result.snapshot is None:
                print(f"jernerics: {result.error}", file=sys.stderr)
                continue
            events.append(
                build_job_resource_event(
                    result.snapshot,
                    study_name=study_name,
                    submission_id=submission_id,
                )
            )
        if not events:
            return
        submission_dir = Path(tracking_dir) / "submission"
        submission_dir.mkdir(parents=True, exist_ok=True)
        # A unique name keeps every capture's cursor independent; the
        # next replay ships any capture this ship could not deliver and
        # then deletes the file.
        path = submission_dir / f"resources-{uuid.uuid4().hex[:8]}.jsonl"
        path.write_text("".join(event.model_dump_json() + "\n" for event in events))
        ship_events_file(path, base_url, api_key)
    except Exception as error:
        print(
            f"jernerics: job resource capture failed: {error!r}",
            file=sys.stderr,
        )


def run_pipeline(
    ctx_path: str,
    chain_depth: int,
    tracking_dir: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
) -> PipelineResult:
    submitted = run_checker(ctx_path=ctx_path, chain_depth=chain_depth)

    if base_url is not None:
        ctx = RetryContext.from_json(Path(ctx_path).read_text())
        # The checker's FAIL tells flush to the journal before this read,
        # so reconciliation always sees the terminal trial state.
        reconcile_paths = reconcile_study(ctx, tracking_dir)
        if submitted:
            # Dead executions of the finished batch stay factually dead
            # even though replacements were just submitted; best-effort
            # ship now, the retry's post-hook replay remains the
            # delivery guarantee.
            for path in reconcile_paths:
                ship_events_file(path, base_url, api_key)
            return PipelineResult.RETRY_SUBMITTED
        # Before replay: replay ships-and-deletes the submission files capture reads.
        capture_job_resources(tracking_dir, ctx.study_name, base_url, api_key)
        # Live trial event logs ship first (a running snapshot must land
        # before its terminal reconciliation); the reconcile snapshots ship
        # last so they close out or conflict with what already landed.
        conflicts: list[ConflictRecord] = []
        skips: list[set[Path] | None] = (
            [set(reconcile_paths), None] if reconcile_paths else [None]
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
        sweep_manifest_blobs(tracking_dir, base_url, api_key)
        _report_scheduler_task_logs(Path(tracking_dir).parent.parent)

    if submitted:
        return PipelineResult.RETRY_SUBMITTED

    return PipelineResult.SWEEP_COMPLETE


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    parser.add_argument("--tracking-dir", required=True)
    parser.add_argument("--server-addr", default=None)
    args = parser.parse_args(argv)

    base_url = None
    api_key = None
    try:
        if args.server_addr:
            ship = resolve_tracking_ship(args.server_addr)
            if ship:
                base_url, api_key = ship
    except TrackingServerSchemeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

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


if __name__ == "__main__":
    main()
