"""Tests for SlurmBackend script generation and job management."""

from unittest.mock import MagicMock

import pytest
from jernerics.backend.slurm_backend import SlurmBackend, expand_slurm_pattern


def _make_backend(**overrides):
    defaults = {
        "host": MagicMock(),
        "container": MagicMock(),
        "syncer": MagicMock(),
        "remote_dir": "~/projects/myproject",
        "partition": "priority",
        "time": "1:00:00",
        "mem": "16G",
        "cpus": 4,
        "max_concurrent_jobs": 10,
        "cache_dir": None,
        "tracking_server": None,
    }
    defaults.update(overrides)

    container = defaults["container"]
    container.wrap = lambda cmd, binds: f"apptainer exec ... {cmd}"
    defaults["container"] = container

    return SlurmBackend(**defaults)


def _wrap_spy(backend):
    """Capture what container.wrap receives and returns."""
    calls = []
    original_wrap = backend.container.wrap

    def spy(cmd, binds):
        result = original_wrap(cmd, binds)
        calls.append((cmd, binds, result))
        return result

    backend.container.wrap = spy
    return calls


class TestGenerateSweepScript:
    def test_sbatch_directives(self):
        backend = _make_backend()
        script = backend._generate_sweep_script(
            setup_command="setup_cmd",
            trial_command="trial_cmd",
            array_spec="1-50%4",
            study_name="study",
            project_name="proj",
            slurm_overrides={"time": "2:00:00"},
        )
        lines = script.splitlines()

        assert lines[0] == "#!/usr/bin/env bash"
        assert "#SBATCH --parsable" in lines[1]
        assert "#SBATCH --array=1-50%4" in lines[2]
        assert any("#SBATCH --partition=priority" in l for l in lines)
        assert any("#SBATCH --time=2:00:00" in l for l in lines)
        assert any("#SBATCH --mem=16G" in l for l in lines)

    def test_array_spec_no_parallel(self):
        backend = _make_backend()
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-100",
            study_name="s",
            project_name="p",
            slurm_overrides={},
        )
        assert "#SBATCH --array=1-100\n" in script

    def test_output_error_patterns_default(self):
        backend = _make_backend(
            remote_dir="~/projects/proj",
            cache_dir=None,
        )
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert (
            "#SBATCH --output=$HOME/projects/proj/.jernerics/logs/%A_%a.out" in script
        )
        assert "#SBATCH --error=$HOME/projects/proj/.jernerics/logs/%A_%a.err" in script

    def test_output_error_patterns_with_cache_dir(self):
        backend = _make_backend(
            cache_dir="~/cache/{project_name}",
        )
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert "#SBATCH --output=$HOME/cache/proj/logs/%A_%a.out" in script
        assert "#SBATCH --error=$HOME/cache/proj/logs/%A_%a.err" in script

    def test_output_error_patterns_custom(self):
        backend = _make_backend()
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={"output": "/custom/%j.out", "error": "/custom/%j.err"},
        )
        assert "#SBATCH --output=/custom/%j.out" in script
        assert "#SBATCH --error=/custom/%j.err" in script

    def test_tilde_expanded_in_sbatch_directives(self):
        backend = _make_backend()
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={"output": "~/logs/%j.out"},
        )
        assert "$HOME/logs/%j.out" in script
        output_line = [
            l for l in script.splitlines() if l.startswith("#SBATCH --output")
        ][0]
        assert "~" not in output_line

    def test_cd_and_env(self):
        backend = _make_backend(remote_dir="~/projects/proj")
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert "cd ~/projects/proj" in script
        assert "REMOTE_DIR=$(cd . && pwd)" in script
        assert "export JERNERICS_HPC=1" in script

    def test_setup_command_wrapped_in_container(self):
        backend = _make_backend()
        wrap_calls = _wrap_spy(backend)
        script = backend._generate_sweep_script(
            setup_command="optuna create study",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert len(wrap_calls) == 2
        assert wrap_calls[0][0] == "optuna create study"
        # flock wraps the entire container invocation in the script
        assert "flock" in script
        assert wrap_calls[0][2] in script

    def test_trial_command_wrapped_in_container(self):
        backend = _make_backend()
        wrap_calls = _wrap_spy(backend)
        backend._generate_sweep_script(
            setup_command="setup",
            trial_command="python -m jernerics.runner",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert wrap_calls[1][0] == "python -m jernerics.runner"

    def test_bind_args_without_cache_dir(self):
        backend = _make_backend(cache_dir=None)
        wrap_calls = _wrap_spy(backend)
        backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        binds = wrap_calls[0][1]
        assert '"${REMOTE_DIR}:/work"' in binds
        assert any(":/cache" in b for b in binds)
        assert len(binds) == 2  # /work and /cache only

    def test_bind_args_with_cache_dir(self):
        backend = _make_backend(cache_dir="~/cache/{project_name}")
        wrap_calls = _wrap_spy(backend)
        backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        binds = wrap_calls[0][1]
        assert any("$HOME/cache/proj:/cache" in b for b in binds)

    def test_tracking_directory_created(self):
        backend = _make_backend()
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="my_study",
            project_name="proj",
            slurm_overrides={},
        )
        assert "mkdir -p ~/projects/myproject/.jernerics/tracking/my_study" in script

    def test_optuna_directory_created(self):
        backend = _make_backend()
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert "mkdir -p ~/projects/myproject/.jernerics/optuna" in script

    def test_flock_guards_setup(self):
        backend = _make_backend()
        backend.container.wrap = lambda cmd, binds: f"apptainer exec ... {cmd}"
        script = backend._generate_sweep_script(
            setup_command="optuna create study",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert "flock" in script
        # flock should come before the trial command
        flock_idx = script.index("flock")
        trial_idx = script.index("apptainer exec ... trial")
        assert flock_idx < trial_idx

    def test_slurm_overrides_dont_include_none(self):
        backend = _make_backend(time=None)
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            slurm_overrides={},
        )
        assert "#SBATCH --time=" not in script


class TestSubmitSweep:
    def test_submits_via_sbatch_parsable(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="12345")
        backend = _make_backend(host=host)

        job_id = backend.submit_sweep(
            setup_command="setup",
            trial_command="trial",
            n_trials=10,
            study_name="study",
            project_name="proj",
        )

        assert job_id == "12345"
        host.run.assert_called_once()
        call_args = host.run.call_args
        cmd = call_args[0][0]
        assert any("sbatch --parsable" in arg for arg in cmd)
        assert call_args[1]["input"] is not None

    def test_submits_from_remote_dir(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="99")
        backend = _make_backend(host=host, remote_dir="~/projects/proj")

        backend.submit_sweep(
            setup_command="setup",
            trial_command="trial",
            n_trials=5,
            study_name="study",
            project_name="proj",
        )

        cmd = host.run.call_args[0][0]
        assert any("cd ~/projects/proj" in arg for arg in cmd)

    def test_raises_on_failure(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1, stderr="sbatch: error")
        backend = _make_backend(host=host)

        with pytest.raises(RuntimeError, match="Failed to submit job"):
            backend.submit_sweep(
                setup_command="setup",
                trial_command="trial",
                n_trials=5,
                study_name="study",
                project_name="proj",
            )


class TestListJobs:
    def test_parses_squeue_output(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0,
            stdout="JOBID\tNAME\tSTATE\n123\tmyjob\tRUNNING\n456\tother\tPENDING",
        )
        backend = _make_backend(host=host)

        jobs = backend.list_jobs()
        assert len(jobs) == 2
        assert jobs[0].job_id == "123"
        assert jobs[0].name == "myjob"
        assert jobs[0].status == "RUNNING"

    def test_empty_jobs(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="")
        backend = _make_backend(host=host)

        jobs = backend.list_jobs()
        assert jobs == []


class TestCancel:
    def test_cancel_success(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0)
        backend = _make_backend(host=host)

        assert backend.cancel("12345") is True
        host.run.assert_called_with(["scancel", "12345"], check=False)

    def test_cancel_failure(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1)
        backend = _make_backend(host=host)

        assert backend.cancel("99999") is False


class TestExpandSlurmPattern:
    def test_job_id(self):
        assert expand_slurm_pattern("log_%j.out", job_id="12345") == "log_12345.out"

    def test_array_task_id(self):
        assert (
            expand_slurm_pattern("log_%A_%a.out", job_id="12345", array_task_id=3)
            == "log_12345_3.out"
        )

    def test_array_job_id_split(self):
        assert expand_slurm_pattern("%j", job_id="12345_6") == "12345_6"
        assert expand_slurm_pattern("%A", job_id="12345_6") == "12345"

    def test_wildcard_replacement(self):
        assert expand_slurm_pattern("%a", replace_unknown_with_wildcard=True) == "*"
        assert expand_slurm_pattern("%a") == "%a"

    def test_job_name(self):
        assert expand_slurm_pattern("%x", job_name="myjob") == "myjob"

    def test_username(self):
        result = expand_slurm_pattern("%u")
        assert result != "%u"
