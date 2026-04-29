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
