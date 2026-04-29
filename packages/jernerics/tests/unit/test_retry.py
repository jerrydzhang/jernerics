import os
from pathlib import Path
from unittest.mock import MagicMock

from jernerics.retry import param_key, plan_retry
from optuna.trial import FrozenTrial, TrialState


def _make_trial(number: int, state: int, params: dict | None = None) -> FrozenTrial:
    trial = MagicMock(spec=FrozenTrial)
    trial.number = number
    trial.state = state
    trial.params = params if params is not None else {"lr": 0.01}
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
