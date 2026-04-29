"""Tests for SlurmBackend script generation and job management."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jernerics.backend.models import SweepSpec
from jernerics.backend.slurm_backend import (
    SlurmBackend,
    _compose_chain,
    expand_slurm_pattern,
)
from jernerics.config import BackendConfig, SharedConfig, SlurmConfig
from jernerics.retry import RetryContext


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
        "heartbeat_interval_s": 60.0,
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
            backend_overrides={"time": "2:00:00"},
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
            backend_overrides={},
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
            backend_overrides={},
        )
        assert "#SBATCH --output=$HOME/.cache/jernerics/proj/logs/%A_%a.out" in script
        assert "#SBATCH --error=$HOME/.cache/jernerics/proj/logs/%A_%a.err" in script

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
            backend_overrides={},
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
            backend_overrides={"output": "/custom/%j.out", "error": "/custom/%j.err"},
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
            backend_overrides={"output": "~/logs/%j.out"},
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
            backend_overrides={},
        )
        assert "cd $HOME/projects/proj" in script
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
            backend_overrides={},
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
            backend_overrides={},
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
            backend_overrides={},
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
            backend_overrides={},
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
            backend_overrides={},
        )
        assert "mkdir -p $HOME/.cache/jernerics/proj/tracking/my_study" in script

    def test_optuna_directory_created(self):
        backend = _make_backend()
        script = backend._generate_sweep_script(
            setup_command="setup",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            backend_overrides={},
        )
        assert "mkdir -p $HOME/.cache/jernerics/proj/optuna" in script

    def test_flock_guards_setup(self):
        backend = _make_backend()
        backend.container.wrap = lambda cmd, binds: f"apptainer exec ... {cmd}"
        script = backend._generate_sweep_script(
            setup_command="optuna create study",
            trial_command="trial",
            array_spec="1-10",
            study_name="study",
            project_name="proj",
            backend_overrides={},
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
            backend_overrides={},
        )
        assert "#SBATCH --time=" not in script


class TestSubmitSweep:
    def test_submits_via_sbatch_parsable(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="12345")
        backend = _make_backend(host=host)

        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="sqlite:////cache/optuna/study.db",
            n_trials=10,
            project_name="proj",
        )
        result = backend.submit_sweep(spec)

        assert result.job_id == "12345"
        assert result.checker_job_id is None
        host.run.assert_called_once()
        call_args = host.run.call_args
        cmd = call_args[0][0]
        assert any("sbatch --parsable" in arg for arg in cmd)
        assert call_args[1]["input"] is not None

    def test_submits_from_remote_dir(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="99")
        backend = _make_backend(host=host, remote_dir="~/projects/proj")

        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="sqlite:////cache/optuna/study.db",
            n_trials=5,
            project_name="proj",
        )
        backend.submit_sweep(spec)

        cmd = host.run.call_args[0][0]
        assert any("cd ~/projects/proj" in arg for arg in cmd)

    def test_raises_on_failure(self):
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1, stderr="sbatch: error")
        backend = _make_backend(host=host)

        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="sqlite:////cache/optuna/study.db",
            n_trials=5,
            project_name="proj",
        )
        with pytest.raises(RuntimeError, match="Failed to submit job"):
            backend.submit_sweep(spec)


class TestListJobs:
    def test_parses_squeue_output(self):
        host = MagicMock()
        host.run.return_value = MagicMock(
            returncode=0,
            stdout="JOBID|NAME|STATE\n123|myjob|RUNNING\n456|other|PENDING",
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


class TestBuildCheckerScript:
    def test_basic_structure(self):
        backend = _make_backend()
        script = backend._build_checker_script(
            ctx_path="/cache/retry/ctx.json",
            chain_depth=1,
            cache_host="/cache",
            partition="priority",
            dependency_job_id="10001",
        )
        assert "#!/usr/bin/env bash" in script
        assert "#SBATCH --parsable" in script
        assert "#SBATCH --partition=priority" in script
        assert "#SBATCH --time=0:10:00" in script
        assert "#SBATCH --mem=1G" in script
        assert "#SBATCH --dependency=afterany:10001" in script
        assert "retry_checker" in script

    def test_output_patterns(self):
        backend = _make_backend()
        script = backend._build_checker_script(
            ctx_path="/cache/retry/ctx.json",
            chain_depth=0,
            cache_host="/cache",
            partition="p",
            dependency_job_id="42",
        )
        assert "#SBATCH --output=/cache/logs/checker_%j.out" in script
        assert "#SBATCH --error=/cache/logs/checker_%j.err" in script

    def test_tilde_expanded(self):
        backend = _make_backend(remote_dir="~/projects/p")
        script = backend._build_checker_script(
            ctx_path="/cache/ctx.json",
            chain_depth=0,
            cache_host="~/cache",
            partition="p",
            dependency_job_id="42",
        )
        assert "~" not in script
        assert "$HOME/cache" in script

    def test_no_dependency(self):
        backend = _make_backend()
        script = backend._build_checker_script(
            ctx_path="/cache/ctx.json",
            chain_depth=0,
            cache_host="/cache",
            partition="p",
        )
        assert "#SBATCH --dependency" not in script


class TestFromConfigSubmitWithRetryCtx:
    """Test the full path the retry checker uses.

    from_config → submit_sweep with retry_ctx.
    """

    @staticmethod
    def _make_config(**overrides):
        shared_defaults = {
            "name": "hpc",
            "type": "slurm",
            "host": "user@hpc",
            "remote_dir": "/scratch/user/proj",
            "cache_dir": "/scratch/user/cache",
            "container_type": "apptainer",
            "heartbeat_interval_s": 60,
        }
        slurm_defaults = {
            "partition": "priority",
            "time": "1:00:00",
            "mem": "16G",
            "cpus": 4,
            "max_concurrent_jobs": 10,
        }
        slurm_keys = set(slurm_defaults)
        for k, v in overrides.items():
            if k in slurm_keys:
                slurm_defaults[k] = v
            else:
                shared_defaults[k] = v

        return BackendConfig(
            shared=SharedConfig(
                name=str(shared_defaults["name"]),
                type=str(shared_defaults["type"]),
                host=str(shared_defaults["host"]),
                remote_dir=str(shared_defaults["remote_dir"]),
                cache_dir=str(shared_defaults["cache_dir"]),
                container_type=str(shared_defaults["container_type"]),
                heartbeat_interval_s=int(shared_defaults["heartbeat_interval_s"]),
            ),
            backend=SlurmConfig(
                partition=str(slurm_defaults["partition"]),
                time=str(slurm_defaults["time"]),
                mem=str(slurm_defaults["mem"]),
                cpus=int(slurm_defaults["cpus"]),
                max_concurrent_jobs=int(slurm_defaults["max_concurrent_jobs"]),
            ),
        )

    def test_from_config_creates_apptainer_container(self):
        from jernerics.backend.components.host import StdoutHost

        config = self._make_config(container_type="apptainer")
        backend = SlurmBackend.from_config(config, host=StdoutHost())
        from jernerics.backend.components.container import Apptainer

        assert isinstance(backend.container, Apptainer)

    def test_from_config_creates_docker_container(self):
        from jernerics.backend.components.host import StdoutHost

        config = self._make_config(container_type="docker")
        backend = SlurmBackend.from_config(config, host=StdoutHost())
        from jernerics.backend.components.container import Docker

        assert isinstance(backend.container, Docker)

    def test_submit_sweep_with_retry_ctx_produces_chain_script(self):
        """Simulate what the checker does: from_config → submit_sweep(retry_ctx).

        Uses a capturing host to intercept the composed script and verify
        it's valid bash that would submit two chained sbatch jobs.
        """
        config = self._make_config()

        captured_input = {}

        class CapturingHost:
            def run(self, command, **kwargs):
                captured_input["command"] = command
                captured_input["input"] = kwargs.get("input", "")
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="10001 10002",
                    stderr="",
                )

            def mkdir(self, path):
                pass

            def file_exists(self, path):
                return False

            def getmtime(self, path):
                return None

            def remove_file(self, path):
                pass

            def write_file(self, path, content):
                pass

        host = CapturingHost()
        backend = SlurmBackend.from_config(config, host=host)

        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="/cache/optuna/study.journal",
            n_trials=3,
            dag_relpath="dag.py",
            config_relpath="config.py",
            project_name="proj",
        )
        retry_ctx = RetryContext(
            study_name="study",
            backend_name="hpc",
            dag_relpath="dag.py",
            config_relpath="config.py",
            ctx_path="/cache/retry/ctx.json",
            chain_depth=1,
        )

        result = backend.submit_sweep(spec, retry_ctx=retry_ctx)

        assert result.job_id == "10001"
        assert result.checker_job_id == "10002"
        assert captured_input["command"] == ["bash"]
        script = captured_input["input"]

        # The composed script contains both jobs chained together
        assert "ARRAY_JOB_ID=$(sbatch --parsable" in script
        assert "CHECKER_JOB_ID=$(sbatch --parsable" in script
        assert "--dependency=afterany:$ARRAY_JOB_ID" in script

        # The array script has the trial command (split across lines with \
        assert "jernerics.runner" in script
        assert "/work/dag.py" in script

        # The checker script has the retry_checker invocation
        assert "python -m jernerics.retry_checker" in script
        assert "--context /cache/retry/ctx.json" in script
        assert "--chain-depth 1" in script

    def test_submit_sweep_without_retry_ctx_uses_sbatch_directly(self):
        """Without retry_ctx, submit_sweep uses cd + sbatch --parsable."""
        config = self._make_config()

        class CapturingHost:
            def run(self, command, **kwargs):
                return subprocess.CompletedProcess(
                    args=list(command),
                    returncode=0,
                    stdout="10001",
                    stderr="",
                )

            def mkdir(self, path):
                pass

            def file_exists(self, path):
                return False

            def getmtime(self, path):
                return None

            def remove_file(self, path):
                pass

            def write_file(self, path, content):
                pass

        host = CapturingHost()
        backend = SlurmBackend.from_config(config, host=host)

        spec = SweepSpec(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="study",
            storage_url="/cache/optuna/study.journal",
            n_trials=3,
        )

        result = backend.submit_sweep(spec)
        assert result.job_id == "10001"
        assert result.checker_job_id is None


class TestComposeChainBashExecution:
    """Test that _compose_chain produces valid bash that executes correctly."""

    def test_composed_script_runs_in_bash(self, tmp_path):
        """Run the composed script through bash with sbatch mocked as echo."""
        mock_sbatch = tmp_path / "sbatch"
        mock_sbatch.write_text("#!/usr/bin/env bash\necho 42")
        mock_sbatch.chmod(0o755)

        array_script = "#!/usr/bin/env bash\n#SBATCH --array=1-3\necho array"
        checker_script = "#!/usr/bin/env bash\necho checker"
        composed = _compose_chain(array_script, checker_script)

        result = subprocess.run(
            ["bash", "-c", composed],
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "PATH": f"{tmp_path}:{__import__('os').environ.get('PATH', '')}",
            },
        )

        assert result.returncode == 0, (
            f"bash failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        output = result.stdout.strip()
        assert output == "42 42", f"Expected '42 42', got: {output}"

    def test_heredoc_expansion_works(self, tmp_path):
        """Verify $ARRAY_JOB_ID is available to the checker sbatch call."""
        mock_sbatch = tmp_path / "sbatch"
        # Second call gets --dependency flag with the captured ID
        mock_sbatch.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *"--dependency"* ]]; then\n'
            '  echo "dep_$2"\n'
            "else\n"
            '  echo "100"\n'
            "fi"
        )
        mock_sbatch.chmod(0o755)

        composed = _compose_chain("echo array", "echo checker")

        result = subprocess.run(
            ["bash", "-c", composed],
            capture_output=True,
            text=True,
            env={
                **__import__("os").environ,
                "PATH": f"{tmp_path}:{__import__('os').environ.get('PATH', '')}",
            },
        )

        assert result.returncode == 0, (
            f"bash failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
        output = result.stdout.strip()
        # First sbatch returns 100, second gets --dependency=afterany:100
        assert output == "100 dep_--dependency=afterany:100", f"Got: {output}"
