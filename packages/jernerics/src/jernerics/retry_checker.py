import argparse
import time
from pathlib import Path

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import TrialState

from jernerics.backend.host import StdoutHost
from jernerics.backend.models import SweepSubmission
from jernerics.backend.submission import assemble_infrastructure, submit_sweep
from jernerics.config import (
    load_backend_config,
    load_config,
    load_tracking_server,
)
from jernerics.retry import (
    plan_retry,
    read_ledger,
    write_ledger,
)


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
    )

    if plan.is_complete:
        return False

    if chain_depth >= backend_config.shared.chain_depth_cap:
        return False

    for trial_id in plan.stale_trial_ids:
        study.tell(trial_id, state=TrialState.FAIL)
        study.enqueue_trial(study.trials[trial_id].params)

    for trial_id in plan.exhausted_trial_ids:
        study.tell(trial_id, state=TrialState.FAIL)

    write_ledger(ledger_path, plan.retry_counts)

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
