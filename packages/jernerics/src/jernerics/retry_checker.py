import argparse
import json
import time
from pathlib import Path

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import TrialState

from jernerics.config import load_backend_config, load_config
from jernerics.retry import RetryContext, generate_checker_script, generate_sweep_script


def _build_setup_command(study_name: str, storage_path: str, direction: str) -> str:
    return (
        f'python -c "'
        f"from optuna.storages.journal import JournalFileBackend, JournalStorage; "
        f"import optuna; "
        f"optuna.create_study("
        f"study_name={study_name!r},"
        f" storage=JournalStorage(JournalFileBackend({storage_path!r})),"
        f" direction={direction!r},"
        f' load_if_exists=True)"'
    )


def _build_trial_command(
    dag_relpath: str,
    config_relpath: str,
    study_name: str,
    storage_path: str,
    project_name: str | None,
    tracking_dir: str,
    tracking_server: str | None,
    heartbeat_interval_s: float,
) -> str:
    args = [
        "python",
        "-m",
        "jernerics.runner",
        f"/work/{dag_relpath}",
        f"/work/{config_relpath}",
        "--study-name",
        study_name,
        "--storage-url",
        storage_path,
        "--tracking-dir",
        tracking_dir,
    ]
    if project_name:
        args.extend(["--project-name", project_name])
    if tracking_server:
        args.extend(["--server-addr", tracking_server])
    if heartbeat_interval_s > 0:
        args.extend(["--heartbeat-interval", str(heartbeat_interval_s)])
    return " \\\n        ".join(args)


def _wrap_apptainer(command: str, cache_host: str) -> str:
    bind_args = f'"${{REMOTE_DIR}}:/work" "{cache_host}:/cache"'
    return (
        f"apptainer exec --fakeroot --contain --nv"
        f" --pwd /work --bind {bind_args}"
        f" container.sif {command}"
    )


def _write_ledger(path: Path, data: dict[int, int]) -> None:
    serialized = {str(k): v for k, v in data.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(serialized, indent=2))
    tmp.rename(path)


def _read_ledger(path: Path) -> dict[int, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {int(k): v for k, v in data.items()}


def _format_stdout(
    array_script: str,
    checker_script: str,
) -> str:
    parts = []

    parts.append("ARRAY_JOB_ID=$(sbatch --parsable <<'EOF'")
    parts.append(array_script)
    parts.append("EOF")
    parts.append(")")
    parts.append("")

    parts.append("sbatch --parsable --dependency=afterany:$ARRAY_JOB_ID <<'EOF'")
    parts.append(checker_script)
    parts.append("EOF")

    return "\n".join(parts)


def run_checker(ctx_path: str, chain_depth: int) -> None:
    ctx = RetryContext.from_json(Path(ctx_path).read_text())

    project_dir = Path("/work")
    backend_config = load_backend_config(ctx.backend_name, project_dir)

    if not backend_config.cache_dir:
        cache_host = f"{backend_config.remote_dir}/.jernerics"
    else:
        cache_host = backend_config.cache_dir.replace("{project_name}", "").replace(
            "{project-name}", ""
        )

    cache_host = cache_host.replace("~", "$HOME")

    sweep = load_config(f"/work/{ctx.config_relpath}")

    slurm_opts = {
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
    slurm_opts = {k: v for k, v in slurm_opts.items() if v is not None}

    max_parallel = int(
        ctx.cli_overrides.get(
            "max_parallel",
            sweep.slurm.get("max_parallel", backend_config.max_concurrent_jobs),
        )
    )

    storage_path = f"/cache/optuna/{ctx.study_name}.journal"
    tracking_dir = f"/cache/tracking/{ctx.study_name}"
    heartbeats_dir = Path(f"{tracking_dir}/heartbeats")
    ledger_path = Path(f"{tracking_dir}/.retry_ledger.json")

    grace_period = backend_config.grace_period_s
    stale_after = backend_config.stale_after_s
    max_retries = backend_config.max_retries
    chain_depth_cap = backend_config.chain_depth_cap
    heartbeat_interval_s = backend_config.heartbeat_interval_s

    time.sleep(grace_period)

    storage = JournalStorage(JournalFileBackend(storage_path))
    study = optuna.load_study(study_name=ctx.study_name, storage=storage)

    ledger = _read_ledger(ledger_path)

    from jernerics.retry import plan_retry

    plan = plan_retry(
        trials=study.trials,
        heartbeats_dir=heartbeats_dir,
        ledger=ledger,
        target=sweep.n_trials,
        stale_after=stale_after,
        max_retries=max_retries,
        now=time.time(),
    )

    if plan.is_complete:
        return

    if chain_depth >= chain_depth_cap:
        print(
            f"Chain depth cap reached ({chain_depth}/{chain_depth_cap}). "
            "Stopping retry chain.",
        )
        return

    for trial_id in plan.stale_trial_ids:
        trial = study.trials[trial_id]
        study.tell(trial_id, state=TrialState.FAIL)
        study.enqueue_trial(trial.params)

    _write_ledger(ledger_path, plan.retry_counts)

    remote_dir = backend_config.remote_dir.replace("~", "$HOME")
    partition = slurm_opts.get("partition", "priority")

    setup_command = _build_setup_command(ctx.study_name, storage_path, sweep.direction)
    trial_command = _build_trial_command(
        ctx.dag_relpath,
        ctx.config_relpath,
        ctx.study_name,
        storage_path,
        None,
        tracking_dir,
        None,
        heartbeat_interval_s,
    )

    wrapped_setup = _wrap_apptainer(setup_command, cache_host)
    wrapped_trial = _wrap_apptainer(trial_command, cache_host)

    array_size = plan.total_array_size
    if max_parallel > 0:
        array_spec = f"1-{array_size}%{max_parallel}"
    else:
        array_spec = f"1-{array_size}"

    array_script = generate_sweep_script(
        array_spec=array_spec,
        study_name=ctx.study_name,
        cache_host=cache_host,
        remote_dir=remote_dir,
        partition=partition,
        time=slurm_opts.get("time"),
        mem=slurm_opts.get("mem", "16G"),
        slurm_overrides={},
        wrapped_setup=wrapped_setup,
        wrapped_trial=wrapped_trial,
        output_dir=f"{cache_host}/logs",
    )

    next_chain_depth = chain_depth + 1
    checker_cmd = (
        f"python -m jernerics.retry_checker"
        f" --context {ctx_path}"
        f" --chain-depth {next_chain_depth}"
    )
    wrapped_checker = _wrap_apptainer(checker_cmd, cache_host)

    checker_script = generate_checker_script(
        cache_host=cache_host,
        remote_dir=remote_dir,
        partition=partition,
        wrapped_checker=wrapped_checker,
        dependency_job_id="$ARRAY_JOB_ID",
    )

    print(_format_stdout(array_script, checker_script))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--context", required=True)
    parser.add_argument("--chain-depth", type=int, required=True)
    args = parser.parse_args()

    run_checker(ctx_path=args.context, chain_depth=args.chain_depth)
