import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from optuna.trial import FrozenTrial, TrialState


@dataclass
class RetryPlan:
    stale_trial_ids: list[int]
    fresh_needed: int
    total_array_size: int
    is_complete: bool
    retry_counts: dict[int, int] = field(default_factory=dict)


@dataclass
class RetryContext:
    study_name: str
    backend_name: str
    dag_relpath: str
    config_relpath: str
    cli_overrides: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "RetryContext":
        data = json.loads(text)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def plan_retry(
    trials: list[FrozenTrial],
    heartbeats_dir: Path,
    ledger: dict[int, int],
    target: int,
    stale_after: float,
    max_retries: int,
    now: float,
) -> RetryPlan:
    complete = 0
    fresh_running = 0
    waiting = 0
    stale_trial_ids: list[int] = []

    for trial in trials:
        if trial.state == TrialState.COMPLETE or trial.state == TrialState.PRUNED:
            complete += 1
        elif trial.state == TrialState.WAITING:
            waiting += 1
        elif trial.state == TrialState.RUNNING:
            hb_path = heartbeats_dir / f"{trial.number}.heartbeat"
            is_stale = False
            if not hb_path.exists():
                is_stale = True
            else:
                mtime = hb_path.stat().st_mtime
                if now - mtime > stale_after:
                    is_stale = True

            if is_stale:
                current_retries = ledger.get(trial.number, 0)
                if current_retries < max_retries:
                    stale_trial_ids.append(trial.number)
            else:
                fresh_running += 1

    retries_enqueued = len(stale_trial_ids)
    remaining_needed = target - complete - fresh_running - waiting - retries_enqueued
    fresh_needed = max(0, remaining_needed)
    total_array_size = retries_enqueued + fresh_needed

    updated_ledger = dict(ledger)
    for tid in stale_trial_ids:
        updated_ledger[tid] = updated_ledger.get(tid, 0) + 1

    is_complete = total_array_size == 0

    return RetryPlan(
        stale_trial_ids=stale_trial_ids,
        fresh_needed=fresh_needed,
        total_array_size=total_array_size,
        is_complete=is_complete,
        retry_counts=updated_ledger,
    )


def _expand_path(p: str) -> str:
    if p.startswith("~"):
        return "$HOME" + p[1:]
    return p


def generate_sweep_script(
    *,
    array_spec: str,
    study_name: str,
    cache_host: str,
    remote_dir: str,
    partition: str,
    time: str | None,
    mem: str,
    slurm_overrides: dict[str, str],
    wrapped_setup: str,
    wrapped_trial: str,
    output_dir: str,
) -> str:
    cache_host = _expand_path(cache_host)
    remote_dir = _expand_path(remote_dir)
    slurm_opts: dict[str, str] = {
        k: v
        for k, v in {
            "partition": partition,
            "time": time,
            "mem": mem,
            **slurm_overrides,
        }.items()
        if v is not None
    }

    if "output" not in slurm_opts:
        slurm_opts["output"] = f"{cache_host}/logs/%A_%a.out"
    if "error" not in slurm_opts:
        slurm_opts["error"] = f"{cache_host}/logs/%A_%a.err"

    lines = [
        "#!/usr/bin/env bash",
        "#SBATCH --parsable",
        f"#SBATCH --array={array_spec}",
    ]
    for key, value in slurm_opts.items():
        lines.append(f"#SBATCH --{key}={value}")
    lines.append("")

    lines.append(f"mkdir -p {output_dir}")
    lines.append(f"cd {remote_dir}")
    lines.append("REMOTE_DIR=$(cd . && pwd)")
    lines.append("export JERNERICS_HPC=1")

    lines.append("")
    lines.append(f"mkdir -p {cache_host}/optuna")
    lines.append(f"flock {cache_host}/optuna/init.lock {wrapped_setup}")
    lines.append(f"mkdir -p {cache_host}/tracking/{study_name}")
    lines.append("")
    lines.append(wrapped_trial)

    return "\n".join(lines)


def generate_checker_script(
    *,
    cache_host: str,
    remote_dir: str,
    partition: str,
    wrapped_checker: str,
    dependency_job_id: str,
) -> str:
    cache_host = _expand_path(cache_host)
    remote_dir = _expand_path(remote_dir)
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        #SBATCH --parsable
        #SBATCH --partition={partition}
        #SBATCH --time=0:10:00
        #SBATCH --mem=1G
        #SBATCH --output={cache_host}/logs/checker_%j.out
        #SBATCH --error={cache_host}/logs/checker_%j.err
        #SBATCH --dependency=afterany:{dependency_job_id}

        cd {remote_dir}
        {wrapped_checker}""")
