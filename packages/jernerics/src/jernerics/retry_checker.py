import argparse
import time
from pathlib import Path

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import TrialState

from jernerics.backend.models import SweepSpec
from jernerics.backend.slurm_backend import SlurmBackend
from jernerics.config import load_backend_config, load_config
from jernerics.retry import (
    RetryContext,
    plan_retry,
    read_ledger,
    write_ledger,
)


def run_checker(ctx_path: str, chain_depth: int) -> None:
    ctx = RetryContext.from_json(Path(ctx_path).read_text())

    project_dir = Path("/work")
    backend_config = load_backend_config(ctx.backend_name, project_dir)
    sweep = load_config(f"/work/{ctx.config_relpath}")

    storage_path = f"/cache/optuna/{ctx.study_name}.journal"
    tracking_dir = f"/cache/tracking/{ctx.study_name}"
    heartbeats_dir = Path(f"{tracking_dir}/heartbeats")
    ledger_path = Path(f"{tracking_dir}/.retry_ledger.json")

    time.sleep(backend_config.grace_period_s)

    storage = JournalStorage(JournalFileBackend(storage_path))
    study = optuna.load_study(study_name=ctx.study_name, storage=storage)

    ledger = read_ledger(ledger_path)

    plan = plan_retry(
        trials=study.trials,
        heartbeats_dir=heartbeats_dir,
        ledger=ledger,
        n_trials=sweep.n_trials,
        stale_after=backend_config.stale_after_s,
        max_retries=backend_config.max_retries,
        now=time.time(),
    )

    if plan.is_complete:
        return

    if chain_depth >= backend_config.chain_depth_cap:
        return

    for trial_id in plan.stale_trial_ids:
        study.tell(trial_id, state=TrialState.FAIL)
        study.enqueue_trial(study.trials[trial_id].params)

    write_ledger(ledger_path, plan.retry_counts)

    max_parallel = int(
        ctx.cli_overrides.get(
            "max_parallel",
            sweep.slurm.get("max_parallel", backend_config.max_concurrent_jobs),
        )
    )

    slurm_overrides = {
        "partition": backend_config.partition,
        "time": backend_config.time,
        "mem": backend_config.mem,
        **{
            k: v
            for k, v in sweep.slurm.items()
            if k not in ("max_parallel", "output", "error")
        },
        **{
            k: v
            for k, v in ctx.cli_overrides.items()
            if k not in ("max_parallel", "output", "error")
        },
    }
    slurm_overrides = {k: v for k, v in slurm_overrides.items() if v is not None}

    retry_spec = SweepSpec(
        dag_path=Path(f"/work/{ctx.dag_relpath}"),
        config_path=Path(f"/work/{ctx.config_relpath}"),
        study_name=ctx.study_name,
        storage_url=storage_path,
        n_trials=plan.total_array_size,
        dag_relpath=ctx.dag_relpath,
        config_relpath=ctx.config_relpath,
        project_name=None,
        max_parallel=max_parallel if max_parallel > 0 else None,
        slurm_overrides=slurm_overrides,
    )

    retry_ctx = RetryContext(
        study_name=ctx.study_name,
        backend_name=ctx.backend_name,
        dag_relpath=ctx.dag_relpath,
        config_relpath=ctx.config_relpath,
        cli_overrides=ctx.cli_overrides,
        ctx_path=ctx_path,
        chain_depth=chain_depth + 1,
    )

    backend = SlurmBackend.from_config(backend_config)
    backend.submit_sweep(retry_spec, direction=sweep.direction, retry_ctx=retry_ctx)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    args = parser.parse_args()

    run_checker(ctx_path=args.context, chain_depth=args.chain_depth)
