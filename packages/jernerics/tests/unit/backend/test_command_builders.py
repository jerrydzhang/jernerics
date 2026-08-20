"""Tests for build_sweep_commands pure function."""

from pathlib import Path
from unittest.mock import MagicMock

from jernerics.backend.command_builders import build_sweep_commands
from jernerics.backend.container import Apptainer, NoContainer
from jernerics.backend.models import SweepSubmission
from jernerics.backend.path_resolver import PathResolver


def _make_paths(
    container=None, remote_dir="/scratch/user/proj", cache_dir=None, project_name="proj"
):
    return PathResolver(
        remote_dir=remote_dir,
        cache_dir=cache_dir,
        container=container or NoContainer(),
        project_name=project_name,
    )


def _make_spec(
    study_name="mystudy",
    trial_relpath="trial.py",
    config_relpath="config.py",
    n_trials=5,
    project_name="proj",
):
    return SweepSubmission(
        trial_path=Path("trial.py"),
        config_path=Path("config.py"),
        study_name=study_name,
        storage_url="sqlite:////cache/optuna/mystudy.journal",
        n_trials=n_trials,
        trial_relpath=trial_relpath,
        config_relpath=config_relpath,
        project_name=project_name,
    )


class TestBuildSweepCommandsBasics:
    def test_returns_three_commands(self):
        spec = _make_spec()
        paths = _make_paths()
        setup, trial, post_hook = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert setup is not None
        assert trial is not None
        assert post_hook is not None

    def test_setup_command_contains_optuna_create_study(self):
        spec = _make_spec()
        paths = _make_paths()
        setup, _, _ = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert "optuna.create_study" in setup
        assert "mystudy" in setup
        assert "minimize" in setup

    def test_trial_command_invokes_runner(self):
        spec = _make_spec()
        paths = _make_paths()
        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert "python -m jernerics.runner" in trial
        assert "mystudy" in trial

    def test_commands_use_host_paths_without_container(self):
        spec = _make_spec()
        paths = _make_paths()
        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert "/scratch/user/proj/trial.py" in trial
        assert "/scratch/user/proj/config.py" in trial

    def test_commands_use_container_paths_with_apptainer(self):
        spec = _make_spec()
        paths = _make_paths(container=Apptainer())
        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=Apptainer(),
            paths=paths,
            direction="minimize",
        )
        assert "/work/trial.py" in trial
        assert "/work/config.py" in trial

    def test_setup_command_wrapped_by_container(self):
        spec = _make_spec()
        container = Apptainer()
        paths = _make_paths(container=container)
        setup, _, _ = build_sweep_commands(
            spec=spec,
            container=container,
            paths=paths,
            direction="minimize",
        )
        assert "apptainer exec" in setup
        assert "container.sif" in setup

    def test_tracking_server_passed_to_trial(self):
        spec = _make_spec()
        paths = _make_paths()
        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
            tracking_server="http://server:8080",
        )
        assert "--server-addr http://server:8080" in trial

    def test_heartbeat_interval_passed_to_trial(self):
        spec = _make_spec()
        paths = _make_paths()
        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
            heartbeat_interval_s=30.0,
        )
        assert "--heartbeat-interval 30.0" in trial

    def test_trial_command_has_no_git_hash_flag(self):
        spec = _make_spec()
        paths = _make_paths()
        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert "--git-hash" not in trial
        assert "--git-hash" not in trial


class TestBuildSweepCommandsAlwaysPostHook:
    """build_sweep_commands always returns a post-hook command."""

    def test_returns_post_hook_without_retry_ctx(self):
        spec = _make_spec()
        paths = _make_paths()
        _, _, post_hook = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert post_hook is not None
        assert "python -m jernerics.post_hook" in post_hook

    def test_post_hook_writes_to_temp_file_without_retry_ctx(self):
        spec = _make_spec()
        paths = _make_paths()
        _, _, post_hook = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert post_hook is not None
        assert "> /tmp/jernerics_mystudy_retry_d0.sh" in post_hook
        assert "bash /tmp/jernerics_mystudy_retry_d0.sh" in post_hook


class TestBuildSweepCommandsWithPostHook:
    def test_returns_post_hook_when_retry_ctx_path_provided(self):
        spec = _make_spec()
        paths = _make_paths()
        _, _, post_hook = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
            retry_ctx_path="/cache/retry/ctx.json",
            chain_depth=2,
        )
        assert post_hook is not None
        assert "python -m jernerics.post_hook" in post_hook
        assert "--context /cache/retry/ctx.json" in post_hook
        assert "--chain-depth 2" in post_hook

    def test_post_hook_writes_to_temp_file(self):
        spec = _make_spec()
        paths = _make_paths()
        _, _, post_hook = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
            retry_ctx_path="/cache/retry/ctx.json",
            chain_depth=1,
        )
        assert post_hook is not None
        assert "> /tmp/jernerics_mystudy_retry_d1.sh" in post_hook
        assert "bash /tmp/jernerics_mystudy_retry_d1.sh" in post_hook

    def test_post_hook_wrapped_by_container(self):
        spec = _make_spec()
        container = Apptainer()
        paths = _make_paths(container=container)
        _, _, post_hook = build_sweep_commands(
            spec=spec,
            container=container,
            paths=paths,
            direction="minimize",
            retry_ctx_path="/cache/retry/ctx.json",
            chain_depth=0,
        )
        assert post_hook is not None
        assert "apptainer exec" in post_hook

    def test_post_hook_always_present(self):
        spec = _make_spec()
        paths = _make_paths()
        _, _, post_hook = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
        )
        assert post_hook is not None
        assert "python -m jernerics.post_hook" in post_hook


class TestBuildSweepCommandsMatchesSlurmOutput:
    """Verify build_sweep_commands produces the same wrapped commands
    that the Slurm adapter renders for an array sweep."""

    def test_wrapped_setup_matches_slurm(self):
        spec = _make_spec()
        container = MagicMock()
        container.wrap = lambda cmd, binds, **kw: f"wrapped({cmd})"
        paths = _make_paths(container=container)

        setup, _, _ = build_sweep_commands(
            spec=spec,
            container=container,
            paths=paths,
            direction="minimize",
        )

        # The setup command should contain the optuna create-study invocation
        assert "wrapped(" in setup
        assert "optuna.create_study" in setup
        assert "mystudy" in setup

    def test_wrapped_trial_matches_slurm(self):
        spec = _make_spec()
        container = MagicMock()
        container.wrap = lambda cmd, binds, **kw: f"wrapped({cmd})"
        paths = _make_paths(container=container)

        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=container,
            paths=paths,
            direction="minimize",
            tracking_server="http://server:8080",
        )

        assert "wrapped(" in trial
        assert "jernerics.runner" in trial
        assert "--server-addr http://server:8080" in trial

    def test_multiline_trial_command(self):
        spec = _make_spec()
        paths = _make_paths()
        _, trial, _ = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
            multiline=True,
        )
        assert " \\\n        " in trial


class TestBuildSweepCommandsEnvPassthrough:
    def test_passes_artifact_env_to_container_wrap(self):
        spec = _make_spec()
        container = MagicMock()
        container.wrap = MagicMock(return_value="wrapped")
        paths = _make_paths(container=container)

        build_sweep_commands(
            spec=spec,
            container=container,
            paths=paths,
            direction="minimize",
            artifact_env={
                "JERNERICS_API_KEY": "secret",
            },
        )

        # Trial command should be wrapped with env vars
        trial_call = container.wrap.call_args_list[1]
        assert trial_call[1]["env"] == {
            "JERNERICS_API_KEY": "secret",
        }

    def test_no_env_when_not_provided(self):
        spec = _make_spec()
        container = MagicMock()
        container.wrap = MagicMock(return_value="wrapped")
        paths = _make_paths(container=container)

        build_sweep_commands(
            spec=spec,
            container=container,
            paths=paths,
            direction="minimize",
        )

        # env is None when not provided
        for call in container.wrap.call_args_list:
            assert call[1].get("env") is None

    def test_post_hook_wrap_receives_artifact_env(self):
        spec = _make_spec()
        container = MagicMock()
        container.wrap = MagicMock(return_value="wrapped")
        paths = _make_paths(container=container)

        build_sweep_commands(
            spec=spec,
            container=container,
            paths=paths,
            direction="minimize",
            retry_ctx_path="/cache/retry/ctx.json",
            chain_depth=0,
            artifact_env={
                "JERNERICS_API_KEY": "secret",
            },
        )

        # Post-hook wrap (3rd call) should have same env vars as trial
        post_hook_call = container.wrap.call_args_list[2]
        assert post_hook_call[1]["env"] == {
            "JERNERICS_API_KEY": "secret",
        }


class TestBuildPostHookTrackingServer:
    def test_includes_server_addr_when_provided(self):
        from jernerics.backend.command_builders import build_post_hook_command

        cmd = build_post_hook_command(
            ctx_path="/cache/ctx.json",
            chain_depth=0,
            tracking_dir="/cache/tracking/study",
            tracking_server="http://server:8080",
        )
        assert "--server-addr http://server:8080" in cmd

    def test_omits_server_addr_when_not_provided(self):
        from jernerics.backend.command_builders import build_post_hook_command

        cmd = build_post_hook_command(
            ctx_path="/cache/ctx.json",
            chain_depth=0,
            tracking_dir="/cache/tracking/study",
        )
        assert "--server-addr" not in cmd
        assert "--storage-path" not in cmd

    def test_post_hook_in_sweep_receives_tracking_server(self):
        spec = _make_spec()
        paths = _make_paths()
        _, _, post_hook = build_sweep_commands(
            spec=spec,
            container=NoContainer(),
            paths=paths,
            direction="minimize",
            tracking_server="http://server:8080",
            retry_ctx_path="/cache/retry/ctx.json",
            chain_depth=0,
        )
        assert post_hook is not None
        assert "--server-addr http://server:8080" in post_hook
