import argparse
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import optuna
from jernerics_schema import SweepId, sweep_id_for
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import FrozenTrial, TrialState

from jernerics.backend.host import StdoutHost
from jernerics.backend.models import SweepSubmission
from jernerics.backend.submission import assemble_infrastructure, submit_sweep
from jernerics.config import (
    load_backend_config,
    load_config,
    load_tracking_server,
)
from jernerics.optuna_mirror import frozen_trial_snapshot, retry_lineage_attrs
from jernerics.retry import (
    plan_retry,
    read_ledger,
    write_ledger,
)
from jernerics.tracking.batch_sync import ship_events_file
from jernerics.tracking.infra import resolve_tracking_ship
from jernerics.tracking.jsonl_io import TrackingWriter


def _append_submission_snapshot(
    trial: FrozenTrial,
    *,
    sweep_id: SweepId,
    submission_dir: Path | None,
) -> None:
    """Best-effort terminal snapshot where the study's replay finds it.

    Where the checker runs without access to the tracking files, emit
    nothing; post-hook reconciliation from the journal covers the state.
    """
    if submission_dir is None:
        return
    try:
        event = frozen_trial_snapshot(
            trial,
            sweep_id=sweep_id,
            recorded_at=datetime.now(UTC),
            event_id=uuid.uuid4(),
        )
        with TrackingWriter(submission_dir / "checker.jsonl") as writer:
            writer.write_event(event)
    except Exception:
        return


def _mark_failed(
    study: optuna.study.Study,
    number: int,
    *,
    sweep_id: SweepId,
    submission_dir: Path | None,
) -> None:
    study.tell(number, state=TrialState.FAIL)
    _append_submission_snapshot(
        study.trials[number], sweep_id=sweep_id, submission_dir=submission_dir
    )


def _enqueue_retry(
    study: optuna.study.Study,
    number: int,
    *,
    sweep_id: SweepId,
    submission_dir: Path | None,
) -> None:
    """Fail the stale trial and enqueue a replacement carrying retry lineage."""
    original = study.trials[number]
    _mark_failed(study, number, sweep_id=sweep_id, submission_dir=submission_dir)
    root_number = original.user_attrs.get("retry_root", number)
    if not isinstance(root_number, int) or isinstance(root_number, bool):
        root_number = number
    root = study.trials[root_number] if root_number != number else original
    lineage = retry_lineage_attrs(study.trials[number], root)
    study.enqueue_trial(original.params, user_attrs=lineage)


def run_checker(ctx_path: str, chain_depth: int) -> bool:
    from jernerics.retry import RetryContext

    ctx = RetryContext.from_json(Path(ctx_path).read_text())

    project_dir = Path(ctx.project_dir)
    backend_config = load_backend_config(ctx.backend_name, project_dir)
    sweep = load_config(f"{ctx.project_dir}/{ctx.config_relpath}")

    storage_path = ctx.storage_path or f"/cache/optuna/{ctx.study_name}.journal"
    tracking_dir = ctx.tracking_dir or f"/cache/tracking/{ctx.study_name}"

    heartbeats_dir = Path(f"{tracking_dir}/heartbeats")
    ledger_path = Path(f"{tracking_dir}/.retry_ledger.json")

    time.sleep(backend_config.shared.grace_period_s)

    storage = JournalStorage(JournalFileBackend(storage_path))
    study = optuna.load_study(study_name=ctx.study_name, storage=storage)

    ledger = read_ledger(ledger_path)

    plan = plan_retry(
        trials=study.trials,
        heartbeats_dir=heartbeats_dir,
        ledger=ledger,
        n_trials=sweep.n_trials,
        stale_after=backend_config.shared.stale_after_s,
        max_retries=backend_config.shared.max_retries,
        now=time.time(),
        fast_fail_threshold_s=backend_config.shared.fast_fail_threshold_s,
        max_fast_failures=backend_config.shared.max_fast_failures,
    )

    if plan.is_complete:
        return False

    if chain_depth >= backend_config.shared.chain_depth_cap:
        return False

    sweep_id = sweep_id_for(ctx.project_name or "", ctx.study_name)
    submission_dir = Path(tracking_dir) / "submission" if tracking_dir else None

    for trial_id in plan.stale_trial_ids:
        _enqueue_retry(
            study, trial_id, sweep_id=sweep_id, submission_dir=submission_dir
        )

    for trial_id in plan.exhausted_trial_ids:
        _mark_failed(study, trial_id, sweep_id=sweep_id, submission_dir=submission_dir)
    for trial_id in plan.fast_failed_trial_ids:
        _mark_failed(study, trial_id, sweep_id=sweep_id, submission_dir=submission_dir)

    write_ledger(ledger_path, plan.retry_counts)

    # Land the checker's failed-trial snapshots before the retry trials
    # stream live: a retry's first snapshot references its retry parent,
    # which must already exist server-side. Best-effort — the retry
    # sweep's post-hook replay remains the delivery guarantee.
    if submission_dir is not None:
        checker_server = ctx.server_addr or load_tracking_server(project_dir)
        ship = resolve_tracking_ship(checker_server or "")
        if ship:
            base_url, api_key = ship
            ship_events_file(submission_dir / "checker.jsonl", base_url, api_key)

    # --- Submit via shared submission module ---

    host = StdoutHost(home=ctx.host_home)
    infra = assemble_infrastructure(
        backend_config, host=host, project_name=ctx.project_name or ""
    )

    tracking_server = ctx.server_addr or load_tracking_server(project_dir)

    # Prepare overrides: experiment from sweep config, CLI from retry context
    experiment_overrides = {
        k: v
        for k, v in sweep.backend_overrides.get(ctx.backend_name, {}).items()
        if k not in ("max_parallel", "output", "error")
    }
    cli_overrides = {
        k: v
        for k, v in ctx.cli_overrides.items()
        if k not in ("max_parallel", "output", "error")
    }

    retry_spec = SweepSubmission(
        trial_path=Path(f"{ctx.project_dir}/{ctx.trial_relpath}"),
        config_path=Path(f"{ctx.project_dir}/{ctx.config_relpath}"),
        study_name=ctx.study_name,
        storage_url=storage_path,
        n_trials=plan.total_array_size,
        trial_relpath=ctx.trial_relpath,
        config_relpath=ctx.config_relpath,
        project_name=ctx.project_name,
        git_hash=ctx.git_hash or None,
        param_overrides=ctx.param_overrides,
    )

    submit_sweep(
        retry_spec,
        infra,
        host=host,
        project_dir=ctx.project_dir,
        project_name=ctx.project_name or "",
        backend_name=ctx.backend_name,
        direction=sweep.direction,
        tracking_server=tracking_server,
        cli_overrides=cli_overrides or None,
        experiment_overrides=experiment_overrides or None,
        heartbeat_interval_s=backend_config.shared.heartbeat_interval_s,
        chain_depth=chain_depth + 1,
    )

    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    args = parser.parse_args()

    run_checker(ctx_path=args.context, chain_depth=args.chain_depth)
