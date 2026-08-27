import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from optuna.trial import FrozenTrial, TrialState


def param_key(params: dict[str, Any]) -> str:
    """Deterministic short key for a parameter combination.

    Two trials with the same params produce the same key, even if they
    have different trial numbers. Used as the ledger key so retry counts
    persist across retried trials.
    """
    serialized = json.dumps(params, sort_keys=True)
    return hashlib.blake2b(serialized.encode(), digest_size=4).hexdigest()


FAST_FAIL_LEDGER_KEY = "__fast_fail__"


@dataclass
class RetryPlan:
    stale_trial_ids: list[int]
    exhausted_trial_ids: list[int]
    fresh_needed: int
    total_array_size: int
    is_complete: bool
    retry_counts: dict[str, int] = field(default_factory=dict)
    fast_failed_trial_ids: list[int] = field(default_factory=list)


@dataclass
class RetryContext:
    study_name: str
    backend_name: str
    trial_relpath: str
    config_relpath: str
    cli_overrides: dict[str, str] = field(default_factory=dict)
    param_overrides: dict[str, Any] = field(default_factory=dict)
    storage_path: str = ""
    tracking_dir: str = ""
    project_dir: str = "/work"
    project_name: str | None = None
    host_home: str = ""
    git_hash: str = ""
    server_addr: str = ""

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
    ledger: dict[str, int],
    n_trials: int,
    stale_after: float,
    max_retries: int,
    now: float,
    fast_fail_threshold_s: int = 30,
    max_fast_failures: int = 3,
) -> RetryPlan:
    complete = 0
    fresh_running = 0
    waiting = 0
    stale_trial_ids: list[int] = []
    stale_param_keys: list[str] = []
    exhausted_trial_ids: list[int] = []
    fast_failed_trial_ids: list[int] = []

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
                started = trial.datetime_start
                is_fast_fail = (
                    started is not None
                    and now - started.timestamp() < fast_fail_threshold_s + stale_after
                )
                if is_fast_fail:
                    fast_failed_trial_ids.append(trial.number)
                else:
                    pkey = param_key(trial.params)
                    current_retries = ledger.get(pkey, 0)
                    if current_retries < max_retries:
                        stale_trial_ids.append(trial.number)
                        stale_param_keys.append(pkey)
                    else:
                        exhausted_trial_ids.append(trial.number)
            else:
                fresh_running += 1

    retries_enqueued = len(stale_trial_ids)

    # Fast-fail circuit breaker. A trial that dies within seconds of starting
    # usually signals a permanent error (missing input file, bad config, import
    # crash) rather than a transient node failure. Without a breaker, every
    # fast failure spawns a fresh replacement that fails the same way, retrying
    # forever with no backoff. We count fast failures globally in the ledger
    # (they are environmental, not parameter-specific); once the count reaches
    # the threshold, fast failures are treated as terminal -- the trial is told
    # FAIL and no replacement is enqueued, so the broken sweep winds down
    # instead of churning. The count resets whenever a trial completed this
    # round and no fast failure occurred, so isolated blips on a long, healthy
    # sweep never accumulate to trip the breaker.
    prior_fast_fails = ledger.get(FAST_FAIL_LEDGER_KEY, 0)
    tripped = prior_fast_fails >= max_fast_failures

    updated_ledger = dict(ledger)
    if complete > 0 and not fast_failed_trial_ids:
        updated_ledger.pop(FAST_FAIL_LEDGER_KEY, None)
    elif fast_failed_trial_ids:
        updated_ledger[FAST_FAIL_LEDGER_KEY] = prior_fast_fails + len(
            fast_failed_trial_ids
        )
        tripped = updated_ledger[FAST_FAIL_LEDGER_KEY] >= max_fast_failures

    for pkey in stale_param_keys:
        updated_ledger[pkey] = updated_ledger.get(pkey, 0) + 1

    # Below the threshold, a fast failure is replaced with a fresh sample (let
    # optuna try different params). Once tripped, fast failures look permanent
    # (missing input file, bad config), so we stop spawning replacement trials
    # entirely -- otherwise the broken sweep churns through identical failures
    # one n_trials batch at a time, forever.
    remaining_needed = n_trials - complete - fresh_running - waiting - retries_enqueued
    fresh_needed = 0 if tripped else max(0, remaining_needed)
    total_array_size = retries_enqueued + fresh_needed
    is_complete = total_array_size == 0

    return RetryPlan(
        stale_trial_ids=stale_trial_ids,
        exhausted_trial_ids=exhausted_trial_ids,
        fresh_needed=fresh_needed,
        total_array_size=total_array_size,
        is_complete=is_complete,
        retry_counts=updated_ledger,
        fast_failed_trial_ids=fast_failed_trial_ids,
    )


def read_ledger(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {str(k): v for k, v in data.items()}


def write_ledger(path: Path, data: dict[str, int]) -> None:
    serialized = {str(k): v for k, v in data.items()}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(serialized, indent=2))
    tmp.rename(path)
