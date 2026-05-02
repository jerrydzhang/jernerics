import argparse
import os
import time
from pathlib import Path

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import TrialState

from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.command_builders import build_sweep_commands
from jernerics.backend.container import (
    Apptainer,
    Docker,
    NoContainer,
)
from jernerics.backend.factory import make_adapter
from jernerics.backend.host import StdoutHost
from jernerics.backend.path_resolver import PathResolver
from jernerics.config import (
    ARTIFACT_ENV_VARS,
    ApptainerConfig,
    PueueConfig,
    SlurmConfig,
    load_backend_config,
    load_config,
)
from jernerics.retry import (
    RetryContext,
    plan_retry,
    read_ledger,
    write_ledger,
)


def _make_container(container_type: str):
    if container_type == "docker":
        return Docker()
    elif container_type == "none":
        return NoContainer()
    return Apptainer()


def run_checker(ctx_path: str, chain_depth: int) -> bool:
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

    # --- Build submission via adapter (not Backend) ---

    host = StdoutHost(home=ctx.host_home)
    adapter = make_adapter(backend_config, host=host)

    shared = backend_config.shared
    remote_dir = shared.remote_dir.replace("~", host.home)
    cache_dir = (
        shared.cache_dir.replace("~", host.home)
        if shared.cache_dir
        else f"{host.home}/.cache/jernerics"
    )
    container = _make_container(shared.container_type)

    build_dir = None
    if isinstance(backend_config.container, ApptainerConfig):
        build_dir = backend_config.container.build_dir
        if build_dir:
            build_dir = build_dir.replace("~", host.home)

    paths = PathResolver(
        remote_dir=remote_dir,
        cache_dir=cache_dir,
        container=container,
        build_dir=build_dir,
        project_name=ctx.project_name or "",
    )

    # Merge overrides: defaults < experiment < CLI
    backend_specific = backend_config.backend
    if isinstance(backend_specific, SlurmConfig):
        defaults = backend_specific.defaults_dict()
    else:
        defaults = {}

    max_parallel = int(
        ctx.cli_overrides.get(
            "max_parallel",
            sweep.backend_overrides.get(ctx.backend_name, {}).get(
                "max_parallel",
                backend_specific.max_concurrent_jobs
                if isinstance(backend_specific, SlurmConfig)
                else backend_specific.parallel
                if isinstance(backend_specific, PueueConfig)
                else 1,
            ),
        )
    )

    merged = {
        **defaults,
        **{
            k: v
            for k, v in sweep.backend_overrides.get(ctx.backend_name, {}).items()
            if k not in ("max_parallel", "output", "error")
        },
        **{
            k: v
            for k, v in ctx.cli_overrides.items()
            if k not in ("max_parallel", "output", "error")
        },
    }
    merged = {k: v for k, v in merged.items() if v is not None}

    # Build retry context for the next chain level
    retry_ctx = RetryContext(
        study_name=ctx.study_name,
        backend_name=ctx.backend_name,
        dag_relpath=ctx.dag_relpath,
        config_relpath=ctx.config_relpath,
        cli_overrides=ctx.cli_overrides,
        storage_path=ctx.storage_path,
        tracking_dir=ctx.tracking_dir,
        project_dir=ctx.project_dir,
        ctx_path=ctx_path,
        chain_depth=chain_depth + 1,
    )

    # Write retry context to host
    cache_host = paths.resolve_cache()
    retry_dir_host = f"{cache_host}/retry"
    host.mkdir(retry_dir_host)
    host_ctx_path = f"{cache_host}/retry/{ctx.study_name}_ctx.json"
    host.write_file(host_ctx_path, retry_ctx.to_json())
    retry_ctx_path = paths.retry_ctx_path(ctx.study_name)

    # Build SweepSubmissionParams directly
    from jernerics.backend.models import SweepSubmission

    retry_spec = SweepSubmission(
        dag_path=Path(f"{ctx.project_dir}/{ctx.dag_relpath}"),
        config_path=Path(f"{ctx.project_dir}/{ctx.config_relpath}"),
        study_name=ctx.study_name,
        storage_url=storage_path,
        n_trials=plan.total_array_size,
        dag_relpath=ctx.dag_relpath,
        config_relpath=ctx.config_relpath,
        project_name=ctx.project_name,
        max_parallel=max_parallel if max_parallel > 0 else None,
        backend_overrides=merged,
    )

    artifact_env = {k: v for k in ARTIFACT_ENV_VARS if (v := os.environ.get(k))}

    wrapped_setup, wrapped_trial, post_hook = build_sweep_commands(
        retry_spec,
        container,
        paths,
        direction=sweep.direction,
        tracking_server=None,
        heartbeat_interval_s=shared.heartbeat_interval_s,
        retry_ctx_path=retry_ctx_path,
        chain_depth=chain_depth + 1,
        multiline=True,
        artifact_env=artifact_env or None,
    )

    params = SweepSubmissionParams(
        setup_command=wrapped_setup,
        trial_command=wrapped_trial,
        post_hook_command=post_hook,
        n_trials=plan.total_array_size,
        study_name=ctx.study_name,
        log_dir=f"{cache_host}/logs",
        cache_dir=cache_host,
        max_parallel=max_parallel if max_parallel > 0 else None,
        overrides=merged,
    )

    adapter.submit_sweep(params)

    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    args = parser.parse_args()

    run_checker(ctx_path=args.context, chain_depth=args.chain_depth)
