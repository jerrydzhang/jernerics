import os
from pathlib import Path
from unittest.mock import MagicMock

from jernerics.retry import generate_checker_script, generate_sweep_script, plan_retry
from optuna.trial import FrozenTrial, TrialState


def _make_trial(number: int, state: TrialState) -> FrozenTrial:
    trial = MagicMock(spec=FrozenTrial)
    trial.number = number
    trial.state = state
    trial.params = {"lr": 0.01}
    return trial


def _write_heartbeat(hb_dir: Path, trial_number: int, age: float, now: float) -> None:
    hb_path = hb_dir / f"{trial_number}.heartbeat"
    hb_path.touch()
    mtime = now - age
    os.utime(hb_path, (mtime, mtime))


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
            target=3,
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
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            target=4,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 5 in plan.stale_trial_ids
        assert plan.total_array_size == 2  # 1 retry + 1 fresh
        assert plan.fresh_needed == 1
        assert not plan.is_complete

    def test_stale_running_increments_ledger(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={5: 1},
            target=3,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert plan.retry_counts[5] == 2

    def test_stale_running_exhausted_retries(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={5: 3},
            target=3,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 5 not in plan.stale_trial_ids
        assert plan.total_array_size == 2  # stale-exhausted doesn't count, need 2 fresh
        assert plan.fresh_needed == 2


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
            target=4,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        assert 5 not in plan.stale_trial_ids
        assert plan.total_array_size == 2  # 2 fresh needed (4 - 1 complete - 1 running)
        assert plan.fresh_needed == 2


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
            target=3,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert 5 in plan.stale_trial_ids
        assert plan.total_array_size == 2  # 1 retry + 1 fresh


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
            target=4,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.total_array_size == 2  # 4 - 1 complete - 1 waiting = 2
        assert plan.fresh_needed == 2

    def test_waiting_plus_fresh_running_plus_complete(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 2, age=10, now=now)

        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(1, TrialState.WAITING),
            _make_trial(2, TrialState.RUNNING),
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            target=5,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        # 5 - 1 complete - 1 waiting - 1 fresh running = 2 fresh
        assert plan.total_array_size == 2
        assert plan.fresh_needed == 2


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
            target=3,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        # FAIL doesn't count as complete, so 3 - 1 = 2 needed
        assert plan.total_array_size == 2
        assert plan.fresh_needed == 2


class TestPlanRetryLedgerDoubleEnqueue:
    def test_no_double_enqueue_across_iterations(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        now = 1000.0
        _write_heartbeat(hb_dir, 5, age=200, now=now)

        # Trial 5 was already enqueued in a previous checker iteration
        # It's still RUNNING because the new array job hasn't started yet
        trials = [
            _make_trial(0, TrialState.COMPLETE),
            _make_trial(5, TrialState.RUNNING),
        ]

        # Simulate: first iteration already put trial 5 in the ledger
        # But WAITING trials from enqueue already count, so we need to
        # test that the ledger is used for the max_retries cap
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={5: 2},
            target=3,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        # retry_count=2 < max_retries=3, so it can be retried once more
        assert 5 in plan.stale_trial_ids
        assert plan.retry_counts[5] == 3


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
            target=2,
            stale_after=120,
            max_retries=3,
            now=1000.0,
        )
        assert plan.is_complete
        assert plan.total_array_size == 0


class TestPlanRetryEdgeCases:
    def test_zero_target(self, tmp_path):
        hb_dir = tmp_path / "heartbeats"
        hb_dir.mkdir()
        plan = plan_retry(
            trials=[],
            heartbeats_dir=hb_dir,
            ledger={},
            target=0,
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
            _make_trial(2, TrialState.RUNNING),  # fresh
        ]
        plan = plan_retry(
            trials=trials,
            heartbeats_dir=hb_dir,
            ledger={},
            target=3,
            stale_after=120,
            max_retries=3,
            now=now,
        )
        # 3 - 2 complete - 1 running = 0
        assert plan.is_complete
        assert plan.fresh_needed == 0


class TestGenerateSweepScript:
    def test_basic_structure(self):
        script = generate_sweep_script(
            array_spec="1-50%4",
            study_name="my_study",
            cache_host="/cache/proj",
            remote_dir="~/projects/proj",
            partition="priority",
            time="1:00:00",
            mem="16G",
            slurm_overrides={},
            wrapped_setup="apptainer exec ... setup",
            wrapped_trial="apptainer exec ... trial",
            output_dir="/cache/proj/logs",
        )
        lines = script.splitlines()
        assert lines[0] == "#!/usr/bin/env bash"
        assert "#SBATCH --parsable" in script
        assert "#SBATCH --array=1-50%4" in script
        assert "#SBATCH --partition=priority" in script
        assert "#SBATCH --time=1:00:00" in script
        assert "#SBATCH --mem=16G" in script

    def test_default_output_error_patterns(self):
        script = generate_sweep_script(
            array_spec="1-10",
            study_name="s",
            cache_host="/cache/p",
            remote_dir="~/p",
            partition="p",
            time="1:00:00",
            mem="16G",
            slurm_overrides={},
            wrapped_setup="setup",
            wrapped_trial="trial",
            output_dir="/cache/p/logs",
        )
        assert "#SBATCH --output=/cache/p/logs/%A_%a.out" in script
        assert "#SBATCH --error=/cache/p/logs/%A_%a.err" in script

    def test_custom_output_error_patterns(self):
        script = generate_sweep_script(
            array_spec="1-10",
            study_name="s",
            cache_host="/cache",
            remote_dir="~/p",
            partition="p",
            time="1:00:00",
            mem="16G",
            slurm_overrides={"output": "/custom/%j.out", "error": "/custom/%j.err"},
            wrapped_setup="setup",
            wrapped_trial="trial",
            output_dir="/custom",
        )
        assert "#SBATCH --output=/custom/%j.out" in script
        assert "#SBATCH --error=/custom/%j.err" in script

    def test_tilde_expanded(self):
        script = generate_sweep_script(
            array_spec="1-10",
            study_name="s",
            cache_host="~/cache",
            remote_dir="~/p",
            partition="p",
            time="1:00:00",
            mem="16G",
            slurm_overrides={},
            wrapped_setup="setup",
            wrapped_trial="trial",
            output_dir="$HOME/cache/logs",
        )
        assert "~" not in script
        assert "$HOME/cache" in script

    def test_setup_and_trial_commands_present(self):
        script = generate_sweep_script(
            array_spec="1-10",
            study_name="s",
            cache_host="/cache",
            remote_dir="~/p",
            partition="p",
            time="1:00:00",
            mem="16G",
            slurm_overrides={},
            wrapped_setup="apptainer exec ... setup_cmd",
            wrapped_trial="apptainer exec ... trial_cmd",
            output_dir="/cache/logs",
        )
        assert "flock /cache/optuna/init.lock apptainer exec ... setup_cmd" in script
        assert "apptainer exec ... trial_cmd" in script

    def test_none_time_excluded(self):
        script = generate_sweep_script(
            array_spec="1-10",
            study_name="s",
            cache_host="/cache",
            remote_dir="~/p",
            partition="p",
            time=None,
            mem="16G",
            slurm_overrides={},
            wrapped_setup="setup",
            wrapped_trial="trial",
            output_dir="/cache/logs",
        )
        assert "#SBATCH --time=" not in script


class TestGenerateCheckerScript:
    def test_basic_structure(self):
        script = generate_checker_script(
            cache_host="/cache",
            remote_dir="~/projects/p",
            partition="priority",
            wrapped_checker="apptainer exec ... checker_cmd",
            dependency_job_id="10001",
        )
        assert "#!/usr/bin/env bash" in script
        assert "#SBATCH --parsable" in script
        assert "#SBATCH --partition=priority" in script
        assert "#SBATCH --time=0:10:00" in script
        assert "#SBATCH --mem=1G" in script
        assert "#SBATCH --dependency=afterany:10001" in script
        assert "apptainer exec ... checker_cmd" in script

    def test_output_patterns(self):
        script = generate_checker_script(
            cache_host="/cache",
            remote_dir="~/p",
            partition="p",
            wrapped_checker="checker",
            dependency_job_id="42",
        )
        assert "#SBATCH --output=/cache/logs/checker_%j.out" in script
        assert "#SBATCH --error=/cache/logs/checker_%j.err" in script

    def test_tilde_expanded(self):
        script = generate_checker_script(
            cache_host="~/cache",
            remote_dir="~/p",
            partition="p",
            wrapped_checker="checker",
            dependency_job_id="42",
        )
        assert "~" not in script
        assert "$HOME/cache" in script

    def test_no_dependency(self):
        script = generate_checker_script(
            cache_host="/cache",
            remote_dir="~/p",
            partition="p",
            wrapped_checker="checker",
        )
        assert "#SBATCH --dependency" not in script
