"""Tests for SlurmAdapter."""

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from jernerics.backend.adapter import SchedulerAdapter
from jernerics.backend.host import StdoutHost
from jernerics.backend.slurm.adapter import (
    SLURM_TERMINAL_STATES,
    SlurmAdapter,
    SlurmSubmitError,
)
from jernerics.config import BackendConfig, ExitCode, SharedConfig, SlurmConfig


def _make_adapter(host=None, **overrides):
    defaults: dict[str, Any] = {
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


def _checker_body(script: str) -> str:
    return script.split("<<'EOF'\n")[2].split("\nEOF")[0]


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

    def test_chain_script_guards_submission_failures(self):
        adapter = _make_adapter()
        params = _make_params(post_hook_command="wrapped_checker_cmd")
        script = adapter.render_sweep(params)

        assert '|| { echo "array job submission failed" >&2; exit 1; }' in script
        assert '[ -n "$ARRAY_JOB_ID" ]' in script
        assert (
            "checker submission failed; array job $ARRAY_JOB_ID already queued"
            in script
        )
        assert '[ -n "$CHECKER_JOB_ID" ]' in script

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


class TestRenderOverrideAllowlist:
    def test_unknown_override_key_raises_value_error(self):
        adapter = _make_adapter()
        params = _make_params(overrides={"target": "3200"})

        with pytest.raises(ValueError) as exc_info:
            adapter.render_sweep(params)

        message = str(exc_info.value)
        assert "target" in message
        assert "partition" in message
        assert "cpus-per-task" in message

    def test_multiple_unknown_keys_all_named(self):
        adapter = _make_adapter()
        params = _make_params(overrides={"target": "3200", "foo": "bar"})

        with pytest.raises(ValueError) as exc_info:
            adapter.render_sweep(params)

        message = str(exc_info.value)
        assert "foo, target" in message

    def test_every_allowlisted_key_renders_as_directive(self):
        from jernerics.backend.slurm.adapter import SBATCH_OVERRIDE_KEYS

        adapter = _make_adapter()
        for key in sorted(SBATCH_OVERRIDE_KEYS):
            params = _make_params(overrides={key: "debug"})
            script = adapter.render_sweep(params)
            assert f"#SBATCH --{key}=debug" in script

    def test_partition_override_still_renders(self):
        adapter = _make_adapter()
        params = _make_params(overrides={"partition": "debug"})

        script = adapter.render_sweep(params)

        assert "#SBATCH --partition=debug" in script


class TestPostHookProfile:
    GPU_OVERRIDES = {
        "account": "qiy18011",
        "gres": "gpu:1",
        "cpus-per-task": "8",
        "constraint": "a100",
        "exclude": "gpu14",
    }

    def test_default_profile_keeps_partition_time_mem(self):
        adapter = _make_adapter(partition="general")
        params = _make_params(post_hook_command="wrapped_checker_cmd")

        body = _checker_body(adapter.render_sweep(params))

        assert "#SBATCH --partition=general" in body
        assert "#SBATCH --time=0:10:00" in body
        assert "#SBATCH --mem=1G" in body

    def test_gpu_overrides_stay_out_of_post_hook(self):
        adapter = _make_adapter(partition="general-gpu", post_hook_partition="general")
        params = _make_params(
            post_hook_command="wrapped_checker_cmd", overrides=self.GPU_OVERRIDES
        )

        script = adapter.render_sweep(params)
        body = _checker_body(script)
        assert "--dependency=afterany:$ARRAY_JOB_ID" in script
        assert "#SBATCH --partition=general" in body
        for key in self.GPU_OVERRIDES:
            assert f"#SBATCH --{key}=" not in body
        assert "#SBATCH --gres=gpu:1" in script
        assert "#SBATCH --constraint=a100" in script

    def test_post_hook_partition_wins_over_trial_partition(self):
        adapter = _make_adapter(post_hook_partition="general")
        params = _make_params(
            post_hook_command="wrapped_checker_cmd",
            overrides={"partition": "general-gpu", "gres": "gpu:1"},
        )

        body = _checker_body(adapter.render_sweep(params))

        assert "#SBATCH --partition=general" in body
        assert "general-gpu" not in body

    def test_post_hook_partition_defaults_to_trial_partition(self):
        adapter = _make_adapter(partition="general")
        params = _make_params(
            post_hook_command="wrapped_checker_cmd",
            overrides={"partition": "debug"},
        )

        body = _checker_body(adapter.render_sweep(params))

        assert "#SBATCH --partition=debug" in body

    def test_post_hook_time_mem_overridable(self):
        adapter = _make_adapter(post_hook_time="0:30:00", post_hook_mem="4G")
        params = _make_params(post_hook_command="wrapped_checker_cmd")

        body = _checker_body(adapter.render_sweep(params))

        assert "#SBATCH --time=0:30:00" in body
        assert "#SBATCH --mem=4G" in body

    def test_from_config_threads_post_hook_profile(self):
        config = BackendConfig(
            shared=SharedConfig(
                name="hpc",
                type="slurm",
                host="user@hpc",
                remote_dir="/scratch/user/proj",
                cache_dir="/scratch/user/cache",
            ),
            backend=SlurmConfig(
                partition="general-gpu",
                post_hook_partition="general",
                post_hook_time="0:20:00",
                post_hook_mem="2G",
            ),
        )

        adapter = SlurmAdapter.from_config(config, host=StdoutHost())

        assert adapter.post_hook_partition == "general"
        assert adapter.post_hook_time == "0:20:00"
        assert adapter.post_hook_mem == "2G"


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
        del host.emits_scripts
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

    def test_chain_empty_stdout_raises_slurm_submit_error(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0, stdout="", stderr="sbatch: error: invalid partition"
        )
        del host.emits_scripts
        adapter = _make_adapter(host=host)
        params = _make_params(post_hook_command="checker_cmd")

        with pytest.raises(SlurmSubmitError, match="invalid partition"):
            adapter.submit_sweep(params)

    def test_chain_checker_failure_names_queued_array_job(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="checker submission failed; array job 10001 already queued",
        )
        adapter = _make_adapter(host=host)
        params = _make_params(post_hook_command="checker_cmd")

        with pytest.raises(SlurmSubmitError, match="array job 10001"):
            adapter.submit_sweep(params)

    def test_chain_keeps_parsable_cluster_suffix(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="10001;hpc 10002;hpc")
        del host.emits_scripts
        adapter = _make_adapter(host=host)
        params = _make_params(post_hook_command="checker_cmd")

        result = adapter.submit_sweep(params)

        assert result.submissions[0].job_id == "10001;hpc"
        assert result.submissions[1].job_id == "10002;hpc"

    def test_emission_host_returns_empty_id_submission(self):
        adapter = _make_adapter(host=StdoutHost())
        params = _make_params(post_hook_command="checker_cmd")

        result = adapter.submit_sweep(params)

        assert len(result.submissions) == 1
        assert result.submissions[0].job_id == ""
        assert result.submissions[0].n_trials == 10


class TestSubmitJob:
    def test_submits_single_job(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="99999")
        adapter = _make_adapter(host=host)

        job_id = adapter.submit_job("echo hello", name="build")

        assert job_id == "99999"

    def test_raises_slurm_submit_error_on_failure(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1, stderr="sbatch: error: qos")
        adapter = _make_adapter(host=host)

        with pytest.raises(SlurmSubmitError, match="Failed to submit build job"):
            adapter.submit_job("echo hello")

    def test_raises_slurm_submit_error_on_non_numeric_id(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="pending approval")
        adapter = _make_adapter(host=host)

        with pytest.raises(SlurmSubmitError, match="pending approval"):
            adapter.submit_job("echo hello")

    def test_keeps_parsable_cluster_suffix(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="99999;hpc")
        adapter = _make_adapter(host=host)

        assert adapter.submit_job("echo hello") == "99999;hpc"

    def test_exclude_emits_sbatch_directive(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="99999")
        adapter = _make_adapter(host=host, exclude="cn649,cn123")

        adapter.submit_job("echo hello", name="build")

        script = host.run.call_args.kwargs["input"]
        assert "#SBATCH --exclude=cn649,cn123\n" in script

    def test_no_exclude_directive_by_default(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="99999")
        adapter = _make_adapter(host=host)

        adapter.submit_job("echo hello", name="build")

        script = host.run.call_args.kwargs["input"]
        assert "--exclude" not in script


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
    @staticmethod
    def _config(cache_dir):
        return BackendConfig(
            shared=SharedConfig(
                name="hpc",
                type="slurm",
                host="user@hpc",
                remote_dir="/scratch/user/proj",
                cache_dir=cache_dir,
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

    def test_constructs_from_config(self):
        adapter = SlurmAdapter.from_config(
            self._config("/scratch/user/cache"), host=StdoutHost()
        )

        assert adapter.partition == "priority"
        assert adapter.remote_dir == "/scratch/user/proj"
        assert adapter.cache_host == "/scratch/user/cache"
        assert isinstance(adapter, SchedulerAdapter)

    def test_cache_dir_with_template_stripped(self):
        adapter = SlurmAdapter.from_config(
            self._config("/scratch/user/cache/{project_name}"), host=StdoutHost()
        )

        assert adapter.cache_host == "/scratch/user/cache"

    def test_cache_dir_with_hyphen_template_stripped(self):
        adapter = SlurmAdapter.from_config(
            self._config("/scratch/user/cache/{project-name}"), host=StdoutHost()
        )

        assert adapter.cache_host == "/scratch/user/cache"

    def test_no_cache_dir_defaults_to_home(self):
        adapter = SlurmAdapter.from_config(
            self._config(None), host=StdoutHost(home="/home/user")
        )

        assert adapter.cache_host == "/home/user/.cache/jernerics"


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

    def test_get_logs_array_element_uses_base_job_meta(self, tmp_path):
        import json

        meta_dir = tmp_path / "jobs"
        meta_dir.mkdir()
        (meta_dir / "100.json").write_text(
            json.dumps(
                {
                    "output_pattern": "/cache/logs/%A_%a.out",
                    "error_pattern": "/cache/logs/%A_%a.err",
                    "remote_dir": "/scratch/proj",
                    "n_trials": 5,
                }
            )
        )

        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="log output")
        adapter = _make_adapter(host=host)

        adapter.get_logs("100_3", meta={"local_cache_dir": tmp_path, "host": host})

        host.run.assert_called_with(
            ["cat /cache/logs/100_3.out"],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_get_logs_without_meta_uses_cache_host_from_meta(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="log data")
        adapter = _make_adapter(host=host)

        adapter.get_logs(
            "42",
            meta={"local_cache_dir": None, "cache_host": "/cache/proj"},
        )

        host.run.assert_called_with(
            ["cat /cache/proj/logs/42_1.out"],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_get_logs_resolves_slurm_patterns(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="log data")
        adapter = _make_adapter(host=host)

        adapter.get_logs("42", meta={"local_cache_dir": None, "host": host})

    def test_cleanup_is_noop(self):
        adapter = _make_adapter()
        # Slurm has no cleanup — should not raise
        adapter.cleanup()


def _write_meta(tmp_path: Path, job_id: str, meta: dict) -> None:
    meta_dir = tmp_path / "jobs"
    meta_dir.mkdir()
    (meta_dir / f"{job_id}.json").write_text(json.dumps(meta))


class TestGetLogsFollow:
    @staticmethod
    def _meta(**overrides: Any) -> dict:
        meta = {
            "output_pattern": "/cache/logs/%A_%a.out",
            "error_pattern": "/cache/logs/%A_%a.err",
            "remote_dir": "/scratch/proj",
            "n_trials": 1,
        }
        meta.update(overrides)
        return meta

    @staticmethod
    def _tail_proc(lines: list[str]) -> MagicMock:
        proc = MagicMock()
        proc.stdout.readline.side_effect = [*lines, ""]
        return proc

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr(
            "jernerics.backend.slurm.adapter.time.sleep", lambda _seconds: None
        )

    def test_follow_running_non_array_streams_from_start(
        self, tmp_path, capsys, monkeypatch
    ):
        _write_meta(tmp_path, "42", self._meta())

        host = MagicMock()
        host.run.side_effect = [
            MagicMock(returncode=0),  # test -f
            MagicMock(returncode=0, stdout="RUNNING"),  # status probe: base id
            MagicMock(returncode=0, stdout="RUNNING"),  # wait poll
            MagicMock(returncode=0, stdout="COMPLETED"),  # wait poll
            MagicMock(returncode=0, stdout="COMPLETED"),  # end-marker state
        ]
        adapter = _make_adapter(host=host)

        tail_proc = self._tail_proc(["first line\n", "second line\n"])
        popen = MagicMock(return_value=tail_proc)
        monkeypatch.setattr(subprocess, "Popen", popen)

        adapter.get_logs(
            "42", meta={"local_cache_dir": tmp_path, "host": host}, follow=True
        )

        popen.assert_called_once()
        argv = popen.call_args.args[0]
        assert argv[2:6] == ["tail", "-n", "+1", "-f"]
        assert argv[6] == "/cache/logs/42_1.out"
        assert popen.call_args.kwargs["stdout"] == subprocess.PIPE

        squeue_calls = [
            call.args[0][0]
            for call in host.run.call_args_list
            if call.args[0][0].startswith("squeue")
        ]
        assert squeue_calls == ["squeue -j 42 -o '%T' -h"] * 4

        out = capsys.readouterr().out
        assert "first line\n" in out
        assert "second line\n" in out
        assert "--- job 42 COMPLETED: follow ended ---" in out
        tail_proc.terminate.assert_called_once()

    @pytest.mark.parametrize("state", sorted(SLURM_TERMINAL_STATES))
    def test_follow_already_terminal_cats_log_without_tail(
        self, tmp_path, capsys, monkeypatch, state
    ):
        _write_meta(tmp_path, "42", self._meta())

        host = MagicMock()
        host.run.side_effect = [
            MagicMock(returncode=0),  # test -f
            MagicMock(returncode=0, stdout=state),  # status probe: terminal
            MagicMock(returncode=0, stdout="full log\n"),  # cat
        ]
        adapter = _make_adapter(host=host)

        popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", popen)

        adapter.get_logs(
            "42", meta={"local_cache_dir": tmp_path, "host": host}, follow=True
        )

        popen.assert_not_called()
        host.run.assert_called_with(
            ["cat /cache/logs/42_1.out"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert "full log\n" in capsys.readouterr().out

    def test_follow_unknown_job_cats_log_without_tail(
        self, tmp_path, capsys, monkeypatch
    ):
        _write_meta(tmp_path, "42", self._meta())

        host = MagicMock()
        host.run.side_effect = [
            MagicMock(returncode=0),  # test -f
            MagicMock(returncode=0, stdout=""),  # squeue base: unknown
            MagicMock(returncode=0, stdout=""),  # sacct base: unknown
            MagicMock(returncode=0, stdout=""),  # squeue base_1: unknown
            MagicMock(returncode=0, stdout=""),  # sacct base_1: unknown
            MagicMock(returncode=0, stdout="purged log\n"),  # cat
        ]
        adapter = _make_adapter(host=host)

        popen = MagicMock()
        monkeypatch.setattr(subprocess, "Popen", popen)

        adapter.get_logs(
            "42", meta={"local_cache_dir": tmp_path, "host": host}, follow=True
        )

        popen.assert_not_called()
        assert "purged log\n" in capsys.readouterr().out

    def test_follow_wildcard_log_requires_array_index(self, tmp_path, capsys):
        _write_meta(
            tmp_path,
            "42",
            self._meta(
                output_pattern="/cache/logs/%A_%a_%x.out",
                error_pattern="/cache/logs/%A_%a_%x.err",
            ),
        )

        host = MagicMock()
        adapter = _make_adapter(host=host)

        with pytest.raises(SystemExit) as excinfo:
            adapter.get_logs(
                "42", meta={"local_cache_dir": tmp_path, "host": host}, follow=True
            )

        assert excinfo.value.code == ExitCode.GENERAL_ERROR
        assert "--array-index" in capsys.readouterr().out
        host.run.assert_not_called()
