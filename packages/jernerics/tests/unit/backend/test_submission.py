import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jernerics.backend.container import Apptainer, Docker, NoContainer
from jernerics.backend.host import LocalHost
from jernerics.backend.models import JobSubmission, SubmitResult, SweepSubmission
from jernerics.backend.path_resolver import PathResolver
from jernerics.config import (
    ApptainerConfig,
    BackendConfig,
    DockerConfig,
    PueueConfig,
    SharedConfig,
    SlurmConfig,
)
from jernerics_schema import (
    JobSnapshotEvent,
    SubmissionSnapshotEvent,
    SweepSnapshotEvent,
)


@pytest.fixture(autouse=True)
def _no_artifact_env(monkeypatch):
    """Keep the ambient tracking key out of tests unless they set one."""
    monkeypatch.delenv("JERNERICS_API_KEY", raising=False)


def _slurm_apptainer_config() -> BackendConfig:
    return BackendConfig(
        shared=SharedConfig(
            name="hpc",
            type="slurm",
            host="user@hpc.example.edu",
            remote_dir="~/experiments/proj",
            cache_dir="~/.cache/jernerics",
            container_type="apptainer",
            heartbeat_interval_s=60,
        ),
        backend=SlurmConfig(),
        container=ApptainerConfig(build_dir="/tmp/build"),
    )


def _mock_host():
    host = MagicMock()
    host.home = "/home/user"
    return host


class TestSubmitSweep:
    def _make_infra(self, adapter=None):
        from jernerics.backend.submission import SweepInfrastructure

        adapter = adapter or MagicMock()
        container = NoContainer()
        paths = PathResolver(
            remote_dir="/scratch/proj",
            cache_dir="/scratch/cache",
            container=container,
            project_name="proj",
        )
        return SweepInfrastructure(adapter=adapter, container=container, paths=paths)

    def test_delegates_to_adapter_and_returns_result(self):
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="42", n_trials=5)]
        )
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )
        host = MagicMock()
        host.home = "/home/user"

        result = submit_sweep(
            spec,
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
        )

        adapter.submit_sweep.assert_called_once()
        assert result is not None
        assert result.submissions[0].job_id == "42"

    def test_merges_overrides_with_time_normalization(self):
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="42", n_trials=5)]
        )
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )
        host = MagicMock()
        host.home = "/home/user"

        submit_sweep(
            spec,
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
            experiment_overrides={"time": "4:00:00", "mem": "32G"},
            cli_overrides={"partition": "gpu"},
        )

        params = adapter.submit_sweep.call_args[0][0]
        assert params.overrides["time"] == "4:00:00"
        assert params.overrides["mem"] == "32G"
        assert params.overrides["partition"] == "gpu"

    def test_filters_none_overrides(self):
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="42", n_trials=5)]
        )
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )
        host = MagicMock()
        host.home = "/home/user"

        submit_sweep(
            spec,
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
            experiment_overrides={"time": "none"},
        )

        params = adapter.submit_sweep.call_args[0][0]
        assert "time" not in params.overrides

    def test_extracts_max_parallel_from_overrides(self):
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="42", n_trials=5)]
        )
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )
        host = MagicMock()
        host.home = "/home/user"

        submit_sweep(
            spec,
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
            cli_overrides={"max_parallel": "4", "mem": "16G"},
        )

        params = adapter.submit_sweep.call_args[0][0]
        assert params.max_parallel == 4
        assert "max_parallel" not in params.overrides
        assert params.overrides["mem"] == "16G"

    def test_writes_retry_context_with_correct_fields(self):
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="42", n_trials=5)]
        )
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
            trial_relpath="trial.py",
            config_relpath="config.py",
        )
        host = MagicMock()
        host.home = "/home/user"

        submit_sweep(
            spec,
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
            cli_overrides={"mem": "32G"},
            chain_depth=2,
        )

        # Verify host.write_file called for retry context
        write_calls = [
            c for c in host.write_file.call_args_list if "_ctx.json" in str(c)
        ]
        assert len(write_calls) == 1

        import json

        ctx = json.loads(write_calls[0][0][1])
        assert ctx["study_name"] == "mystudy"
        assert ctx["backend_name"] == "hpc"
        assert ctx["project_name"] == "proj"
        assert ctx["host_home"] == "/home/user"
        # chain_depth and ctx_path excluded from serialization
        assert ctx["cli_overrides"] == {"mem": "32G"}

        # Verify host.mkdir was called for retry dir
        mkdir_calls = [str(c) for c in host.mkdir.call_args_list]
        assert any("/retry" in c for c in mkdir_calls)

    def test_dry_run_returns_rendered_string(self):
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.render_sweep.return_value = "#!/bin/bash\necho hello"
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )
        host = MagicMock()
        host.home = "/home/user"

        result = submit_sweep(
            spec,
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
            dry_run=True,
        )

        assert result == "#!/bin/bash\necho hello"
        adapter.render_sweep.assert_called_once()
        adapter.submit_sweep.assert_not_called()

    def test_dry_run_unknown_override_fails_loudly(self):
        from jernerics.backend.slurm.adapter import SlurmAdapter
        from jernerics.backend.submission import submit_sweep

        adapter = SlurmAdapter(
            MagicMock(),
            remote_dir="/scratch/proj",
            partition="priority",
            time="1:00:00",
            mem="16G",
            cpus=4,
            max_concurrent_jobs=10,
            cache_host="/cache",
        )
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )
        host = MagicMock()
        host.home = "/home/user"

        with pytest.raises(ValueError) as exc_info:
            submit_sweep(
                spec,
                infra,
                host=host,
                project_dir="/work",
                project_name="proj",
                backend_name="hpc",
                direction="minimize",
                experiment_overrides={"target": "3200"},
                dry_run=True,
            )

        assert "target" in str(exc_info.value)

    def test_writes_param_overrides_to_retry_context(self):
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = None
        infra = self._make_infra(adapter)
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
            param_overrides={"target": 3200},
        )
        host = MagicMock()
        host.home = "/home/user"

        submit_sweep(
            spec,
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
        )

        import json

        write_calls = [
            c for c in host.write_file.call_args_list if "_ctx.json" in str(c)
        ]
        ctx = json.loads(write_calls[0][0][1])
        assert ctx["param_overrides"] == {"target": 3200}


class TestSubmissionEventEmission:
    def _make_infra(self, adapter, cache_dir):
        from jernerics.backend.submission import SweepInfrastructure

        return SweepInfrastructure(
            adapter=adapter,
            container=NoContainer(),
            paths=PathResolver(
                remote_dir="/scratch/proj",
                cache_dir=cache_dir,
                container=NoContainer(),
                project_name="",
            ),
        )

    def test_emits_one_job_snapshot_per_submission(self, tmp_path):
        from jernerics.backend.host import LocalHost
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[
                JobSubmission(job_id="123", n_trials=5, role="trials"),
                JobSubmission(job_id="124", n_trials=0, role="checker"),
            ]
        )
        infra = self._make_infra(adapter, str(tmp_path))
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("configs/sweep.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
            trial_relpath="trial.py",
            config_relpath="configs/sweep.py",
            project_name="proj",
            git_hash="abc123",
        )

        result = submit_sweep(
            spec,
            infra,
            host=LocalHost(),
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
        )

        assert result is not None
        path = (
            tmp_path
            / "tracking"
            / "mystudy"
            / "submission"
            / f"{spec.submission_id}.jsonl"
        )
        from jernerics.tracking.jsonl_io import TrackingReader

        with TrackingReader(path) as reader:
            events = list(reader)
        assert [event.tag for event in events] == [
            "sweep_snapshot",
            "submission_snapshot",
            "job_snapshot",
            "job_snapshot",
        ]
        sweep, submission = events[0], events[1]
        assert isinstance(sweep, SweepSnapshotEvent)
        assert isinstance(submission, SubmissionSnapshotEvent)
        jobs = [event for event in events if isinstance(event, JobSnapshotEvent)]
        assert len(jobs) == 2
        assert sweep.name == "mystudy"
        assert submission.backend == "hpc"
        assert submission.expected_trials == 5
        assert submission.git_hash == "abc123"
        assert submission.config_source == "configs/sweep.py"
        assert submission.submission_id.hex == spec.submission_id
        assert [job.scheduler_job_id for job in jobs] == ["123", "124"]
        assert [job.role for job in jobs] == ["trials", "checker"]

    def test_no_events_without_project_name(self, tmp_path):
        from unittest.mock import MagicMock

        from jernerics.backend.host import LocalHost
        from jernerics.backend.submission import submit_sweep

        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="123", n_trials=5)]
        )
        infra = self._make_infra(adapter, str(tmp_path))
        spec = SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )

        submit_sweep(
            spec,
            infra,
            host=LocalHost(),
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
        )

        assert not (tmp_path / "tracking").exists()

    def test_submission_ids_are_fresh_per_submission(self):
        first = SweepSubmission(
            trial_path=Path("t.py"),
            config_path=Path("c.py"),
            study_name="s",
            storage_url="u",
            n_trials=1,
        )
        second = SweepSubmission(
            trial_path=Path("t.py"),
            config_path=Path("c.py"),
            study_name="s",
            storage_url="u",
            n_trials=1,
        )
        assert first.submission_id != second.submission_id
        assert len(first.submission_id) == 32


class TestAssembleInfrastructure:
    def test_slurm_apptainer_returns_correct_types(self):
        from jernerics.backend.submission import assemble_infrastructure

        config = _slurm_apptainer_config()
        host = _mock_host()

        infra = assemble_infrastructure(config, host=host, project_name="proj")

        assert isinstance(infra.container, Apptainer)
        assert infra.paths.remote_dir == "/home/user/experiments/proj"
        assert infra.paths.cache_dir == "/home/user/.cache/jernerics"

    def test_slurm_apptainer_build_dir_expanded(self):
        from jernerics.backend.submission import assemble_infrastructure

        config = _slurm_apptainer_config()
        host = _mock_host()

        infra = assemble_infrastructure(config, host=host, project_name="proj")

        # build_dir "/tmp/build" has no ~ so stays the same
        assert infra.paths.resolve_build_dir("proj") == "/tmp/build/proj"

    def test_pueue_docker_gpu_flag(self):
        from jernerics.backend.submission import assemble_infrastructure

        config = BackendConfig(
            shared=SharedConfig(
                name="scimlab",
                type="pueue",
                host="user@scimlab.example.edu",
                remote_dir="~/experiments/proj",
                container_type="docker",
            ),
            backend=PueueConfig(parallel=2),
            container=DockerConfig(gpu=True),
        )
        host = _mock_host()

        infra = assemble_infrastructure(config, host=host, project_name="proj")

        assert isinstance(infra.container, Docker)
        assert infra.container.gpu is True

    def test_pueue_docker_no_gpu(self):
        from jernerics.backend.submission import assemble_infrastructure

        config = BackendConfig(
            shared=SharedConfig(
                name="scimlab",
                type="pueue",
                host="user@scimlab.example.edu",
                remote_dir="~/experiments/proj",
                container_type="docker",
            ),
            backend=PueueConfig(),
            container=DockerConfig(gpu=False),
        )
        host = _mock_host()

        infra = assemble_infrastructure(config, host=host, project_name="proj")

        assert isinstance(infra.container, Docker)
        assert infra.container.gpu is False

    def test_no_container_work_prefix_is_remote_dir(self):
        from jernerics.backend.submission import assemble_infrastructure

        config = BackendConfig(
            shared=SharedConfig(
                name="local",
                type="pueue",
                remote_dir="/home/user/experiments/proj",
                container_type="none",
            ),
            backend=PueueConfig(),
        )
        host = _mock_host()

        infra = assemble_infrastructure(config, host=host, project_name="proj")

        assert isinstance(infra.container, NoContainer)
        assert infra.paths.work_prefix == "/home/user/experiments/proj"


class RecordingHost(LocalHost):
    """LocalHost that records every executed command."""

    def __init__(self):
        super().__init__()
        self.commands = []

    def run(self, command, **kwargs):
        self.commands.append(list(command))
        return super().run(command, **kwargs)


class TestSubmitSweepEnvFile:
    """The tracking API key lands in a 0600 env file, never in argv."""

    def _make_infra(self, adapter, cache_dir):
        from jernerics.backend.submission import SweepInfrastructure

        container = Apptainer()
        return SweepInfrastructure(
            adapter=adapter,
            container=container,
            paths=PathResolver(
                remote_dir="/scratch/proj",
                cache_dir=cache_dir,
                container=container,
                project_name="",
            ),
        )

    def _make_spec(self):
        return SweepSubmission(
            trial_path=Path("trial.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=5,
        )

    def test_writes_env_file_and_references_path(self, tmp_path, monkeypatch):
        from jernerics.backend.submission import submit_sweep

        sentinel = "argv-should-never-contain-this"
        monkeypatch.setenv("JERNERICS_API_KEY", sentinel)
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="42", n_trials=5)]
        )
        cache = tmp_path / "cache"
        infra = self._make_infra(adapter, str(cache))
        host = RecordingHost()

        submit_sweep(
            self._make_spec(),
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
        )

        env_path = cache / "tracking" / "env"
        assert env_path.read_text() == f"JERNERICS_API_KEY={sentinel}\n"
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
        chmod_cmds = [c for c in host.commands if c[:2] == ["chmod", "600"]]
        assert len(chmod_cmds) == 1
        assert chmod_cmds[0][2].startswith(str(cache / "tracking" / "env.tmp."))
        assert host.commands.count(["mv", "-f", chmod_cmds[0][2], str(env_path)]) == 1

        params = adapter.submit_sweep.call_args[0][0]
        for command in (
            params.setup_command,
            params.trial_command,
            params.post_hook_command,
        ):
            assert sentinel not in command
        assert f"--env-file {env_path}" in params.trial_command
        assert f"--env-file {env_path}" in params.post_hook_command
        assert "--env-file" not in params.setup_command

    def test_dry_run_references_env_file_without_writing(self, tmp_path, monkeypatch):
        from jernerics.backend.submission import submit_sweep

        monkeypatch.setenv("JERNERICS_API_KEY", "secret")
        adapter = MagicMock()
        adapter.render_sweep.return_value = "#!/bin/bash\ntrue"
        cache = tmp_path / "cache"
        infra = self._make_infra(adapter, str(cache))
        host = RecordingHost()

        result = submit_sweep(
            self._make_spec(),
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
            dry_run=True,
        )

        assert result == "#!/bin/bash\ntrue"
        env_path = cache / "tracking" / "env"
        assert not env_path.exists()
        assert host.commands == []
        params = adapter.render_sweep.call_args[0][0]
        assert f"--env-file {env_path}" in params.trial_command
        assert "secret" not in params.trial_command

    def test_no_env_file_without_env(self, tmp_path, monkeypatch):
        from jernerics.backend.submission import submit_sweep

        monkeypatch.delenv("JERNERICS_API_KEY", raising=False)
        adapter = MagicMock()
        adapter.submit_sweep.return_value = SubmitResult(
            submissions=[JobSubmission(job_id="42", n_trials=5)]
        )
        cache = tmp_path / "cache"
        infra = self._make_infra(adapter, str(cache))
        host = RecordingHost()

        submit_sweep(
            self._make_spec(),
            infra,
            host=host,
            project_dir="/work",
            project_name="proj",
            backend_name="hpc",
            direction="minimize",
        )

        assert not (cache / "tracking" / "env").exists()
        assert host.commands == []
        params = adapter.submit_sweep.call_args[0][0]
        assert "--env-file" not in params.trial_command


class TestWriteEnvFile:
    def test_writes_sorted_keys_verbatim(self, tmp_path):
        from jernerics.backend.submission import write_env_file

        path = write_env_file(
            LocalHost(), str(tmp_path), {"B_KEY": "v b", "A_KEY": "v a"}
        )

        assert path == str(tmp_path / "tracking" / "env")
        assert (tmp_path / "tracking" / "env").read_text() == "A_KEY=v a\nB_KEY=v b\n"

    def test_raises_when_chmod_fails(self):
        from jernerics.backend.submission import write_env_file

        host = MagicMock()
        host.run.return_value = MagicMock(returncode=1)

        with pytest.raises(RuntimeError, match="chmod 600 failed"):
            write_env_file(host, "/cache", {"JERNERICS_API_KEY": "k"})

    def test_raises_when_mv_fails(self):
        from jernerics.backend.submission import write_env_file

        host = MagicMock()
        host.run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=1)]

        with pytest.raises(RuntimeError, match="mv -f"):
            write_env_file(host, "/cache", {"JERNERICS_API_KEY": "k"})
