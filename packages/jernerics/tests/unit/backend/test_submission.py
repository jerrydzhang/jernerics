from pathlib import Path
from unittest.mock import MagicMock

from jernerics.backend.container import Apptainer, Docker, NoContainer
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
