import json
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

    # Set at submission time, not serialized
    ctx_path: str = ""
    chain_depth: int = 0

    def to_json(self) -> str:
        return json.dumps(
            {
                k: v
                for k, v in self.__dict__.items()
                if k not in ("ctx_path", "chain_depth")
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> "RetryContext":
        data = json.loads(text)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


def plan_retry(
    trials: list[FrozenTrial],
    heartbeats_dir: Path,
    ledger: dict[int, int],
    n_trials: int,
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
    remaining_needed = n_trials - complete - fresh_running - waiting - retries_enqueued
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


def read_ledger(path: Path) -> dict[int, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {int(k): v for k, v in data.items()}


def write_ledger(path: Path, data: dict[int, int]) -> None:
    serialized = {str(k): v for k, v in data.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(serialized, indent=2))
    tmp.rename(path)
