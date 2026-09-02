import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from jernerics.retry import FAST_FAIL_LEDGER_KEY, RetryContext, param_key, plan_retry
from optuna.trial import FrozenTrial, TrialState


def _make_trial(
    number: int,
    state: int,
    params: dict | None = None,
    datetime_start: datetime | None = None,
) -> FrozenTrial:
    trial = MagicMock(spec=FrozenTrial)
    trial.number = number
    trial.state = state
    trial.params = params if params is not None else {"lr": 0.01}
    trial.datetime_start = datetime_start or datetime.fromtimestamp(0, tz=timezone.utc)
    return trial


def _write_heartbeat(hb_dir: Path, trial_number: int, age: float, now: float) -> None:
    hb_path = hb_dir / f"{trial_number}.heartbeat"
    hb_path.touch()
    mtime = now - age
    os.utime(hb_path, (mtime, mtime))


class TestParamKey:
    def test_same_params_same_key(self):
        assert param_key({"lr": 0.01}) == param_key({"lr": 0.01})

    def test_different_params_different_key(self):
        assert param_key({"lr": 0.01}) != param_key({"lr": 0.001})

    def test_key_order_independent(self):
        assert param_key({"lr": 0.01, "dropout": 0.3}) == param_key(
            {"dropout": 0.3, "lr": 0.01}
        )

    def test_key_is_8_chars(self):
        assert len(param_key({"lr": 0.01})) == 8


class TestPlanRetryAllComplete:
    def test_all_complete_is_done(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.COMPLETE),
            _make_trial(2, TrialState.COMPLETE),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=3,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.is_complete
        assert plan.total_array_size == 0
        assert plan.fresh_needed == 0
        assert plan.stale_trial_ids == []


class TestPlanRetryStaleRunning:
    def test_stale_running_enqueued_for_retry(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params = {"lr": 0.01}
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING, params),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 5 in plan.stale_trial_ids
        assert plan.total_array_size == 4  # 1 retry + 3 fresh
        assert plan.fresh_needed == 3
        assert not plan.is_complete

    def test_stale_running_increments_ledger(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params = {"lr": 0.01}
        pkey = param_key(params)
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING, params),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={pkey: 1},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan.retry_counts[pkey] == 2

    def test_stale_running_exhausted_retries(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params = {"lr": 0.01}
        pkey = param_key(params)
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING, params),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={pkey: 3},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 5 not in plan.stale_trial_ids
        assert 5 in plan.exhausted_trial_ids
        assert plan.total_array_size == 5
        assert plan.fresh_needed == 5

    def test_retry_count_persists_across_trial_numbers(self, tmp_path):
        """Same params on a new trial number share the retry count."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params = {"lr": 0.01}
        pkey = param_key(params)

        # Round 1: trial 5 dies, retry count goes 0 -> 1
        _write_heartbeat(hb_dir, 5, age=200, now=now)
        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING, params),
        ]
        plan1 = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan1.retry_counts[pkey] == 1
        assert 5 in plan1.stale_trial_ids

        # Round 2: retried as trial 10 (same params), also dies
        _write_heartbeat(hb_dir, 10, age=200, now=now)
        trials2 = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.FAIL, params),
            _make_trial(10, TrialState.RUNNING, params),
        ]
        plan2 = plan_retry(
            trials=trials2,
            heartbeats_dir=hb_dir,
            ledger=plan1.retry_counts,
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan2.retry_counts[pkey] == 2
        assert 10 in plan2.stale_trial_ids

        # Round 3: retried as trial 15, also dies
        _write_heartbeat(hb_dir, 15, age=200, now=now)
        trials3 = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.FAIL, params),
            _make_trial(10, TrialState.FAIL, params),
            _make_trial(15, TrialState.RUNNING, params),
        ]
        plan3 = plan_retry(
            trials=trials3,
            heartbeats_dir=hb_dir,
            ledger=plan2.retry_counts,
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan3.retry_counts[pkey] == 3
        assert 15 in plan3.stale_trial_ids

        # Round 4: retried as trial 20, also dies — exhausted
        _write_heartbeat(hb_dir, 20, age=200, now=now)
        trials4 = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.FAIL, params),
            _make_trial(10, TrialState.FAIL, params),
            _make_trial(15, TrialState.FAIL, params),
            _make_trial(20, TrialState.RUNNING, params),
        ]
        plan4 = plan_retry(
            trials=trials4,
            heartbeats_dir=hb_dir,
            ledger=plan3.retry_counts,
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 20 not in plan4.stale_trial_ids
        assert 20 in plan4.exhausted_trial_ids
        assert plan4.fresh_needed == 5  # give up, submit fresh

    def test_different_params_tracked_independently(self, tmp_path):
        """Two stale trials with different params get separate ledger entries."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params_a = {"lr": 0.001}
        params_b = {"lr": 0.01}
        pkey_a = param_key(params_a)
        pkey_b = param_key(params_b)

        _write_heartbeat(hb_dir, 3, age=200, now=now)
        _write_heartbeat(hb_dir, 7, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(3, TrialState.RUNNING, params_a),
            _make_trial(7, TrialState.RUNNING, params_b),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan.retry_counts[pkey_a] == 1
        assert plan.retry_counts[pkey_b] == 1
        assert plan.stale_trial_ids == [3, 7]

    def test_different_params_exhausted_independently(self, tmp_path):
        """One param combo exhausted, the other still retryable."""
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params_a = {"lr": 0.001}
        params_b = {"lr": 0.01}
        pkey_a = param_key(params_a)
        pkey_b = param_key(params_b)

        _write_heartbeat(hb_dir, 3, age=200, now=now)
        _write_heartbeat(hb_dir, 7, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(3, TrialState.RUNNING, params_a),
            _make_trial(7, TrialState.RUNNING, params_b),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={pkey_a: 3, pkey_b: 1},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        # params_a exhausted (count=3), params_b still retryable
        assert 3 not in plan.stale_trial_ids
        assert 7 in plan.stale_trial_ids


class TestPlanRetryFastFail:
    def test_short_runtime_goes_to_fast_failed(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                {"lr": 0.01},
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
        )
        assert 5 in plan.fast_failed_trial_ids
        assert 5 not in plan.stale_trial_ids
        assert 5 not in plan.exhausted_trial_ids

    def test_long_runtime_goes_to_stale(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                {"lr": 0.01},
                datetime_start=datetime.fromtimestamp(now - 600, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
        )
        assert 5 in plan.stale_trial_ids
        assert 5 not in plan.fast_failed_trial_ids

    def test_short_runtime_takes_precedence_over_exhausted(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params = {"lr": 0.01}
        pkey = param_key(params)
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                params,
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={pkey: 3},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
        )
        assert 5 in plan.fast_failed_trial_ids
        assert 5 not in plan.exhausted_trial_ids
        assert plan.retry_counts.get(pkey, 0) == 3

    def test_fast_failed_not_counted_in_ledger(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params = {"lr": 0.01}
        pkey = param_key(params)
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                params,
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
        )
        assert 5 in plan.fast_failed_trial_ids
        assert plan.retry_counts.get(pkey, 0) == 0
        assert plan.total_array_size == 5


class TestPlanRetryFastFailCircuitBreaker:
    """Repeated instant failures trip a breaker that halts the retry churn."""

    def test_fast_fail_counted_below_threshold(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                {"lr": 0.01},
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
            max_fast_failures=3,
        )
        # Count is recorded so future rounds can detect repetition...
        assert plan.retry_counts[FAST_FAIL_LEDGER_KEY] == 1
        # ...but below the threshold the failure is still replaced.
        assert plan.total_array_size == 5

    def test_at_threshold_stops_spawning_replacements(self, tmp_path):
        # Prior fast failures already sit at the threshold, so the next fast
        # failure trips the breaker: no fresh trial is enqueued to replace it.
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                {"lr": 0.01},
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={FAST_FAIL_LEDGER_KEY: 2},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
            max_fast_failures=3,
        )
        assert 5 in plan.fast_failed_trial_ids
        assert plan.total_array_size == 0
        assert plan.is_complete

    def test_resets_on_healthy_round(self, tmp_path):
        # A completion with no fast failure this round clears the counter, so
        # isolated blips on a long, healthy sweep never accumulate.
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=10, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                {"lr": 0.01},
                datetime_start=datetime.fromtimestamp(now - 10, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={FAST_FAIL_LEDGER_KEY: 2},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
            max_fast_failures=3,
        )
        assert FAST_FAIL_LEDGER_KEY not in plan.retry_counts

    def test_persists_without_completion(self, tmp_path):
        # The missing-file scenario: nothing ever completes, so the count
        # survives across rounds and eventually trips the breaker.
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(
                5,
                TrialState.RUNNING,
                {"lr": 0.01},
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={FAST_FAIL_LEDGER_KEY: 1},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
            max_fast_failures=3,
        )
        assert plan.retry_counts[FAST_FAIL_LEDGER_KEY] == 2

    def test_simultaneous_fast_fails_trip_immediately(self, tmp_path):
        # Several jobs dying at once is overwhelming evidence of a permanent
        # error: the breaker trips within a single round.
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        for n in (5, 6, 7):
            _write_heartbeat(hb_dir, n, age=200, now=now)

        trials = [
            _make_trial(
                n,
                TrialState.RUNNING,
                {"lr": 0.01 * n},
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            )
            for n in (5, 6, 7)
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
            max_fast_failures=3,
        )
        assert plan.retry_counts[FAST_FAIL_LEDGER_KEY] == 3
        assert plan.total_array_size == 0
        assert plan.is_complete

    def test_below_threshold_no_replacement_inflation(self, tmp_path):
        # Confirm the pre-fix behavior is preserved: a single fast failure
        # below the threshold still spawns one fresh replacement.
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(
                5,
                TrialState.RUNNING,
                {"lr": 0.01},
                datetime_start=datetime.fromtimestamp(now - 5, tz=timezone.utc),
            ),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
            fast_fail_threshold_s=30,
            max_fast_failures=3,
        )
        assert plan.fresh_needed == 5


class TestPlanRetryFreshRunning:
    def test_fresh_running_reduces_remaining(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=30, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 5 not in plan.stale_trial_ids
        assert plan.total_array_size == 4
        assert plan.fresh_needed == 4


class TestPlanRetryNoHeartbeat:
    def test_running_no_heartbeat_is_stale(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert 5 in plan.stale_trial_ids
        assert plan.total_array_size == 5


class TestPlanRetryWaitingTrials:
    def test_waiting_reduces_remaining(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.WAITING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.total_array_size == 4
        assert plan.fresh_needed == 4

    def test_waiting_plus_fresh_running_plus_complete(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=10, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.WAITING),
            _make_trial(5, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan.total_array_size == 3
        assert plan.fresh_needed == 3


class TestPlanRetryFailTrials:
    def test_fail_trials_not_counted_as_complete(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.FAIL),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.total_array_size == 5
        assert plan.fresh_needed == 5


class TestPlanRetryDeterministicFail:
    """Grid sweeps route in-runner FAILs through the same-params ledger."""

    def test_fail_under_budget_plans_same_params_lineage_retry(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        params = {"lr": 0.02}
        pkey = param_key(params)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.FAIL, params),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=1000.0,
            deterministic=True,
        )
        assert plan.failed_retry_trial_ids == [1]
        assert plan.failed_exhausted_trial_ids == []
        assert plan.retry_counts[pkey] == 1
        assert plan.fresh_needed == 4
        assert plan.total_array_size == 5
        assert not plan.is_complete

    def test_fail_at_budget_is_exhausted_and_occupies_slot(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        params = {"lr": 0.02}
        pkey = param_key(params)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.FAIL, params),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={pkey: 3},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=1000.0,
            deterministic=True,
        )
        assert plan.failed_retry_trial_ids == []
        assert plan.failed_exhausted_trial_ids == [1]
        assert plan.retry_counts[pkey] == 3
        assert plan.fresh_needed == 4
        assert plan.total_array_size == 4

    def test_exhausted_fail_makes_single_combo_sweep_terminal(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        params = {"lr": 0.02}
        pkey = param_key(params)

        plan = plan_retry(
            trials=[_make_trial(0, TrialState.FAIL, params)],
            heartbeats_dir=hb_dir,
            ledger={pkey: 2},
            n_trials=1,
            stale_after=120,
            max_retries=2,
            now=1000.0,
            deterministic=True,
        )
        assert plan.failed_exhausted_trial_ids == [0]
        assert plan.fresh_needed == 0
        assert plan.total_array_size == 0
        assert plan.is_complete

    def test_fail_stochastic_default_refills_fresh_without_ledger(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        params = {"lr": 0.02}
        pkey = param_key(params)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.FAIL, params),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.failed_retry_trial_ids == []
        assert plan.failed_exhausted_trial_ids == []
        assert pkey not in plan.retry_counts
        assert plan.fresh_needed == 5
        assert plan.total_array_size == 5

    def test_fail_with_existing_retry_is_not_replanned(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        params = {"lr": 0.02}
        pkey = param_key(params)

        settled = _make_trial(1, TrialState.FAIL, params)
        settled.user_attrs = {"retry_of": 0, "retry_root": 0, "retry_index": 1}
        replacement = _make_trial(2, TrialState.FAIL, params)
        replacement.user_attrs = {"retry_of": 1, "retry_root": 0, "retry_index": 2}

        trials = [
            _make_trial(0, TrialState.COMPLETE, {"lr": 0.01}),
            settled,
            replacement,
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={pkey: 1},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=1000.0,
            deterministic=True,
        )
        assert plan.failed_retry_trial_ids == [2]
        assert plan.retry_counts[pkey] == 2
        assert plan.total_array_size == 5

    def test_fail_under_budget_with_tripped_breaker_still_retries_same_params(
        self, tmp_path
    ):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        params = {"lr": 0.02}
        pkey = param_key(params)

        plan = plan_retry(
            trials=[_make_trial(0, TrialState.FAIL, params)],
            heartbeats_dir=hb_dir,
            ledger={FAST_FAIL_LEDGER_KEY: 3, pkey: 0},
            n_trials=2,
            stale_after=120,
            max_retries=3,
            now=1000.0,
            deterministic=True,
        )
        assert plan.failed_retry_trial_ids == [0]
        assert plan.fresh_needed == 0
        assert plan.total_array_size == 1

    def test_permanent_fail_stops_after_max_retries_across_cycles(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        params_a = {"lr": 0.01}
        params_b = {"lr": 0.02}
        pkey_b = param_key(params_b)

        trials = [
            _make_trial(0, TrialState.COMPLETE, params_a),
            _make_trial(1, TrialState.FAIL, params_b),
        ]
        plan1 = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=2,
            stale_after=120,
            max_retries=2,
            now=1000.0,
            deterministic=True,
        )
        assert plan1.failed_retry_trial_ids == [1]
        assert plan1.retry_counts[pkey_b] == 1
        assert plan1.total_array_size == 1

        first_retry = _make_trial(2, TrialState.FAIL, params_b)
        first_retry.user_attrs = {"retry_of": 1, "retry_root": 1, "retry_index": 1}
        trials = [trials[0], trials[1], first_retry]
        plan2 = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger=plan1.retry_counts,
            n_trials=2,
            stale_after=120,
            max_retries=2,
            now=1000.0,
            deterministic=True,
        )
        assert plan2.failed_retry_trial_ids == [2]
        assert plan2.retry_counts[pkey_b] == 2
        assert plan2.total_array_size == 1

        second_retry = _make_trial(3, TrialState.FAIL, params_b)
        second_retry.user_attrs = {"retry_of": 2, "retry_root": 1, "retry_index": 2}
        trials = [*trials, second_retry]
        plan3 = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger=plan2.retry_counts,
            n_trials=2,
            stale_after=120,
            max_retries=2,
            now=1000.0,
            deterministic=True,
        )
        assert plan3.failed_retry_trial_ids == []
        assert plan3.failed_exhausted_trial_ids == [3]
        assert plan3.retry_counts[pkey_b] == 2
        assert plan3.total_array_size == 0
        assert plan3.is_complete


class TestPlanRetryLedgerDoubleEnqueue:
    def test_no_double_enqueue_across_iterations(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        params = {"lr": 0.01}
        pkey = param_key(params)
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING, params),
        ]

        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={pkey: 2},
            n_trials=6,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 5 in plan.stale_trial_ids
        assert plan.retry_counts[pkey] == 3


class TestPlanRetryPrunedTrials:
    def test_pruned_counted_as_complete(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.PRUNED),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=2,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.is_complete
        assert plan.total_array_size == 0


class TestPlanRetryEdgeCases:
    def test_zero_n_trials(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        plan = plan_retry(
            trials=[],
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=0,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.is_complete

    def test_negative_remaining_means_zero_fresh(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 2, age=10, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.COMPLETE),
            _make_trial(2, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            n_trials=3,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan.is_complete
        assert plan.fresh_needed == 0


class TestRetryContextProjectName:
    def test_project_name_survives_serialization_roundtrip(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
            project_name="sweep-retry",
        )
        restored = RetryContext.from_json(ctx.to_json())
        assert restored.project_name == "sweep-retry"

    def test_project_name_defaults_to_none(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
        )
        assert ctx.project_name is None

    def test_project_none_survives_serialization_roundtrip(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
            project_name=None,
        )
        restored = RetryContext.from_json(ctx.to_json())
        assert restored.project_name is None


class TestRetryContextHostHome:
    def test_host_home_survives_serialization_roundtrip(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
            host_home="/home/jez21005",
        )
        json_str = ctx.to_json()
        assert '"host_home": "/home/jez21005"' in json_str
        restored = RetryContext.from_json(json_str)
        assert restored.host_home == "/home/jez21005"

    def test_host_home_defaults_to_empty(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
        )
        assert ctx.host_home == ""

    def test_host_home_empty_survives_roundtrip(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
            host_home="",
        )
        restored = RetryContext.from_json(ctx.to_json())
        assert restored.host_home == ""


class TestRetryContextServerAddr:
    def test_server_addr_survives_serialization_roundtrip(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
            server_addr="http://gpu-01:8080",
        )
        json_str = ctx.to_json()
        assert '"server_addr": "http://gpu-01:8080"' in json_str
        restored = RetryContext.from_json(json_str)
        assert restored.server_addr == "http://gpu-01:8080"

    def test_server_addr_defaults_to_empty(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
        )
        assert ctx.server_addr == ""

    def test_server_addr_empty_survives_roundtrip(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
            server_addr="",
        )
        restored = RetryContext.from_json(ctx.to_json())
        assert restored.server_addr == ""


class TestRetryContextParamOverrides:
    def test_param_overrides_survive_serialization_roundtrip(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
            param_overrides={"target": 3200},
        )
        restored = RetryContext.from_json(ctx.to_json())
        assert restored.param_overrides == {"target": 3200}

    def test_param_overrides_default_empty(self):
        ctx = RetryContext(
            study_name="test-study",
            backend_name="hpc",
            trial_relpath="trial.py",
            config_relpath="config.py",
        )
        assert ctx.param_overrides == {}

    def test_ctx_json_without_key_defaults_empty(self):
        text = json.dumps(
            {
                "study_name": "test-study",
                "backend_name": "hpc",
                "trial_relpath": "trial.py",
                "config_relpath": "config.py",
            }
        )
        restored = RetryContext.from_json(text)
        assert restored.param_overrides == {}
