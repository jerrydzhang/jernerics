"""Tests for SlurmAdapter."""

from unittest.mock import MagicMock

import pytest
from jernerics.backend.adapter import SchedulerAdapter
from jernerics.backend.slurm.adapter import SlurmAdapter
from jernerics.config import BackendConfig, SharedConfig, SlurmConfig


def _make_adapter(host=None, **overrides):
    defaults = {
        "remote_dir": "/scratch/user/proj",
        "partition": "priority",
        "time": "1:00:00",
        "mem": "16G",
        "cpus": 4,
        "max_concurrent_jobs": 10,
        "cache_host": "/home/user/.cache/jernerics/proj",
    }
    defaults.update(overrides)
    if host is None:
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="12345")
    return SlurmAdapter(host=host, **defaults)


def _make_params(
    setup_command="apptainer exec ... optuna_create_study",
    trial_command="apptainer exec ... jernerics.runner",
    n_trials=10,
    study_name="mystudy",
    log_dir="/cache/logs",
    post_hook_command=None,
    max_parallel=None,
    overrides=None,
):
    from jernerics.backend.adapter import SweepSubmissionParams

    return SweepSubmissionParams(
        setup_command=setup_command,
        trial_command=trial_command,
        n_trials=n_trials,
        study_name=study_name,
        log_dir=log_dir,
        post_hook_command=post_hook_command,
        max_parallel=max_parallel,
        overrides=overrides or {},
    )


class TestRenderSweep:
    def test_produces_sbatch_script(self):
        adapter = _make_adapter()
        params = _make_params()
        script = adapter.render_sweep(params)

        assert script.startswith("#!/usr/bin/env bash")
        assert "#SBATCH --parsable" in script
        assert "#SBATCH --array=1-10%10" in script

    def test_includes_wrapped_setup(self):
        adapter = _make_adapter()
        params = _make_params(setup_command="wrapped_setup_cmd")
        script = adapter.render_sweep(params)

        assert "wrapped_setup_cmd" in script
        assert "flock" in script

    def test_includes_wrapped_trial(self):
        adapter = _make_adapter()
        params = _make_params(trial_command="wrapped_trial_cmd")
        script = adapter.render_sweep(params)

        assert "wrapped_trial_cmd" in script

    def test_slurm_directives_from_config(self):
        adapter = _make_adapter(partition="gpu", time="2:00:00", mem="32G")
        params = _make_params()
        script = adapter.render_sweep(params)

        assert "#SBATCH --partition=gpu" in script
        assert "#SBATCH --time=2:00:00" in script
        assert "#SBATCH --mem=32G" in script

    def test_overrides_override_config(self):
        adapter = _make_adapter(partition="cpu")
        params = _make_params(overrides={"partition": "gpu", "time": "4:00:00"})
        script = adapter.render_sweep(params)

        assert "#SBATCH --partition=gpu" in script
        assert "#SBATCH --time=4:00:00" in script

    def test_no_post_hook_without_command(self):
        adapter = _make_adapter()
        params = _make_params()
        script = adapter.render_sweep(params)

        # No chaining, just the array script
        assert "ARRAY_JOB_ID" not in script
        assert "CHECKER_JOB_ID" not in script

    def test_with_post_hook_produces_chain(self):
        adapter = _make_adapter()
        params = _make_params(post_hook_command="wrapped_checker_cmd")
        script = adapter.render_sweep(params)

        assert "ARRAY_JOB_ID=$(sbatch --parsable" in script
        assert "CHECKER_JOB_ID=$(sbatch --parsable" in script
        assert "--dependency=afterany:$ARRAY_JOB_ID" in script
        assert "wrapped_checker_cmd" in script
        assert "#SBATCH --kill-on-invalid-dep=yes" in script

    def test_max_parallel_none_uses_default(self):
        adapter = _make_adapter(max_concurrent_jobs=5)
        params = _make_params(n_trials=20, max_parallel=None)
        script = adapter.render_sweep(params)

        assert "#SBATCH --array=1-20%5" in script

    def test_max_parallel_zero_no_limit(self):
        adapter = _make_adapter()
        params = _make_params(n_trials=20, max_parallel=0)
        script = adapter.render_sweep(params)

        assert "#SBATCH --array=1-20" in script
        assert "%0" not in script

    def test_output_error_defaults(self):
        adapter = _make_adapter(cache_host="/cache")
        params = _make_params()
        script = adapter.render_sweep(params)

        assert "#SBATCH --output=/cache/logs/%A_%a.out" in script
        assert "#SBATCH --error=/cache/logs/%A_%a.err" in script

    def test_custom_output_error_patterns(self):
        adapter = _make_adapter()
        params = _make_params(
            overrides={"output": "/custom/%j.out", "error": "/custom/%j.err"}
        )
        script = adapter.render_sweep(params)

        assert "#SBATCH --output=/custom/%j.out" in script
        assert "#SBATCH --error=/custom/%j.err" in script

    def test_cd_and_env(self):
        adapter = _make_adapter(remote_dir="~/projects/proj")
        params = _make_params()
        script = adapter.render_sweep(params)

        assert "cd $HOME/projects/proj" in script
        assert "REMOTE_DIR=$(cd . && pwd)" in script
        assert "export JERNERICS_HPC=1" in script

    def test_tracking_and_optuna_dirs_created(self):
        adapter = _make_adapter(cache_host="/cache")
        params = _make_params(study_name="exp1")
        script = adapter.render_sweep(params)

        assert "mkdir -p /cache/optuna" in script
        assert "mkdir -p /cache/tracking/exp1" in script


class TestSubmitSweep:
    def test_submits_and_returns_job_id(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="12345")
        adapter = _make_adapter(host=host)
        params = _make_params(n_trials=5)

        result = adapter.submit_sweep(params)

        assert len(result.submissions) == 1
        assert result.submissions[0].job_id == "12345"
        assert result.submissions[0].n_trials == 5

    def test_with_post_hook_returns_both_ids(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="10001 10002")
        adapter = _make_adapter(host=host)
        params = _make_params(post_hook_command="checker_cmd")

        result = adapter.submit_sweep(params)

        assert len(result.submissions) == 2
        assert result.submissions[0].job_id == "10001"
        assert result.submissions[0].n_trials == 10
        assert result.submissions[1].job_id == "10002"
        assert result.submissions[1].n_trials == 0

    def test_raises_on_failure(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1, stderr="sbatch: error")
        adapter = _make_adapter(host=host)

        with pytest.raises(RuntimeError, match="Failed to submit"):
            adapter.submit_sweep(_make_params())


class TestSubmitJob:
    def test_submits_single_job(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="99999")
        adapter = _make_adapter(host=host)

        job_id = adapter.submit_job("echo hello", name="build")

        assert job_id == "99999"


class TestJobLifecycle:
    def test_list_jobs(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0, stdout="JOBID|NAME|STATE\n123|myjob|RUNNING"
        )
        adapter = _make_adapter(host=host)

        jobs = adapter.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].job_id == "123"

    def test_cancel(self):
        host = MagicMock()
        host.run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # squeue: no dependents
            MagicMock(returncode=0),  # scancel
        ]
        adapter = _make_adapter(host=host)

        assert adapter.cancel("12345") is True
        assert host.run.call_args_list[1].args[0] == ["scancel", "12345"]

    def test_cancel_also_cancels_dependent_checker(self):
        host = MagicMock()
        host.run.side_effect = [
            MagicMock(returncode=0, stdout="67890|afterany:12345\n11111|(null)\n"),
            MagicMock(returncode=0),  # scancel
        ]
        adapter = _make_adapter(host=host)

        assert adapter.cancel("12345") is True
        assert host.run.call_args_list[1].args[0] == ["scancel", "12345", "67890"]

    def test_cancel_ignores_unrelated_jobs(self):
        host = MagicMock()
        host.run.side_effect = [
            MagicMock(
                returncode=0,
                stdout="67890|afterany:99999\n11111|afterany:123456\n",
            ),
            MagicMock(returncode=0),  # scancel
        ]
        adapter = _make_adapter(host=host)

        assert adapter.cancel("12345") is True
        assert host.run.call_args_list[1].args[0] == ["scancel", "12345"]

    def test_cancel_all(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        adapter = _make_adapter(host=host)

        assert adapter.cancel_all() is True

    def test_get_status_running(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="RUNNING")
        adapter = _make_adapter(host=host)

        assert adapter.get_status("123") == "RUNNING"

    def test_get_status_completed(self):
        host = MagicMock()
        # squeue returns nothing, sacct returns state
        host.run.side_effect = [
            MagicMock(returncode=0, stdout=""),
            MagicMock(returncode=0, stdout="COMPLETED"),
        ]
        adapter = _make_adapter(host=host)

        assert adapter.get_status("123") == "COMPLETED"

    def test_wait_for_completion(self):
        host = MagicMock()
        host.run.side_effect = [
            MagicMock(returncode=0, stdout="RUNNING"),
            MagicMock(returncode=0, stdout="COMPLETED"),
        ]
        adapter = _make_adapter(host=host)

        result = adapter.wait_for_completion("123", poll_interval=0.01)
        assert result is True


class TestFromConfig:
    def test_constructs_from_config(self):
        from jernerics.backend.host import StdoutHost

        config = BackendConfig(
            shared=SharedConfig(
                name="hpc",
                type="slurm",
                host="user@hpc",
                remote_dir="/scratch/user/proj",
                cache_dir="/scratch/user/cache",
                container_type="apptainer",
                heartbeat_interval_s=60,
            ),
            backend=SlurmConfig(
                partition="priority",
                time="1:00:00",
                mem="16G",
                cpus=4,
                max_concurrent_jobs=10,
            ),
        )
        adapter = SlurmAdapter.from_config(config, host=StdoutHost())

        assert adapter.partition == "priority"
        assert adapter.remote_dir == "/scratch/user/proj"
        assert isinstance(adapter, SchedulerAdapter)


class TestGetLogs:
    def test_get_logs_with_meta(self, tmp_path):
        import json

        meta_dir = tmp_path / "jobs"
        meta_dir.mkdir()
        meta = {
            "output_pattern": "/cache/logs/%A_%a.out",
            "error_pattern": "/cache/logs/%A_%a.err",
            "remote_dir": "/scratch/proj",
            "n_trials": 3,
        }
        (meta_dir / "123.json").write_text(json.dumps(meta))

        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="log output")
        adapter = _make_adapter(host=host)

        adapter.get_logs("123", meta={"local_cache_dir": tmp_path, "host": host})

    def test_get_logs_resolves_slurm_patterns(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="log data")
        adapter = _make_adapter(host=host)

        adapter.get_logs("42", meta={"local_cache_dir": None, "host": host})

    def test_cleanup_is_noop(self):
        adapter = _make_adapter()
        # Slurm has no cleanup — should not raise
        adapter.cleanup()
