"""Tests for PueueBackend script generation and submission."""

from unittest.mock import MagicMock

from jernerics.backend.pueue_backend import PueueBackend


def _make_backend(**overrides):
    defaults = {
        "host": MagicMock(),
        "container": MagicMock(),
        "remote_dir": "$HOME/projects/myproject",
        "cache_dir": "$HOME/.cache/jernerics",
        "tracking_server": None,
        "parallel": 2,
        "syncer": MagicMock(),
        "heartbeat_interval_s": 60.0,
        "auto_retry": False,
        "stale_after_s": 120,
        "grace_period_s": 120,
        "max_retries": 3,
        "chain_depth_cap": 20,
        "build_dir": None,
        "project_name": "",
    }
    defaults.update(overrides)
    return PueueBackend(**defaults)


class TestGenerateSubmitJob:
    def test_uses_single_quote_wrapping(self):
        """The build script is passed to pueue via bash -c '...'."""
        backend = _make_backend()
        script = backend.generate_submit_job("echo hello", name="build")
        assert "bash -e -c 'echo hello'" in script
        # No nested heredoc
        assert "JERNERICS_EOF" not in script
        assert "$(cat <<" not in script

    def test_extracts_task_id(self):
        """Output extracts the pueue task ID from pueue add output."""
        backend = _make_backend()
        script = backend.generate_submit_job("echo hello", name="build")
        assert "grep -oE '[0-9]+'" in script
        assert "echo $BUILD_ID" in script

    def test_uses_label(self):
        backend = _make_backend()
        script = backend.generate_submit_job("echo hi", name="my-build")
        assert "--label my-build" in script

    def test_accepts_log_dir_without_error(self):
        """Pueue generate_submit_job accepts log_dir but ignores it."""
        backend = _make_backend()
        # Should not raise
        script = backend.generate_submit_job("echo hi", name="build", log_dir="/cache")
        # log_dir should NOT appear in the output — pueue doesn't use it
        assert "/cache" not in script

    def test_multiline_script_in_single_quotes(self):
        """Multi-line build scripts are wrapped in single quotes."""
        build_script = "set -e\ncd /home/proj\ndocker build -t img ."
        backend = _make_backend()
        script = backend.generate_submit_job(build_script, name="build")
        assert "bash -e -c 'set -e\ncd /home/proj\ndocker build -t img .'" in script


class TestTrackingDirIsContainerAware:
    """_generate_submit_script must use container-aware tracking_dir."""

    def _make_spec(self):
        from pathlib import Path

        from jernerics.backend.models import SweepSubmission

        return SweepSubmission(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=3,
        )

    def test_with_apptainer_uses_cache_prefix(self):
        from jernerics.backend.components.container import Apptainer

        backend = _make_backend(container=Apptainer())
        script = backend._generate_submit_script(self._make_spec())
        assert "--tracking-dir" in script
        assert "/cache/tracking/mystudy" in script

    def test_with_no_container_uses_host_cache_path(self):
        from jernerics.backend.components.container import NoContainer

        backend = _make_backend(
            container=NoContainer(), cache_dir="$HOME/.cache/jernerics"
        )
        script = backend._generate_submit_script(self._make_spec())
        assert "--tracking-dir" in script
        assert "$HOME/.cache/jernerics/tracking/mystudy" in script


class TestCheckerScriptGeneration:
    """_generate_submit_script with retry_ctx must avoid pipe-to-bash."""

    def _make_spec(self):
        from pathlib import Path

        from jernerics.backend.models import SweepSubmission

        return SweepSubmission(
            dag_path=Path("dag.py"),
            config_path=Path("config.py"),
            study_name="mystudy",
            storage_url="/cache/optuna/mystudy.journal",
            n_trials=3,
        )

    def _make_retry_ctx(self, chain_depth=0):
        from jernerics.retry import RetryContext

        return RetryContext(
            study_name="mystudy",
            backend_name="local",
            dag_relpath="dag.py",
            config_relpath="config.py",
            ctx_path="/cache/retry/mystudy_ctx.json",
            chain_depth=chain_depth,
        )

    def test_no_pipe_to_bash(self):
        backend = _make_backend(auto_retry=True)
        backend.container.wrap = lambda cmd, binds: f"wrapped({cmd})"
        script = backend._generate_submit_script(
            self._make_spec(), retry_ctx=self._make_retry_ctx()
        )
        assert "| bash" not in script

    def test_writes_retry_script_to_file_then_executes(self):
        backend = _make_backend(auto_retry=True)
        backend.container.wrap = lambda cmd, binds: f"wrapped({cmd})"
        script = backend._generate_submit_script(
            self._make_spec(), retry_ctx=self._make_retry_ctx(chain_depth=0)
        )
        assert "> /tmp/jernerics_mystudy_retry_d0.sh" in script
        assert "bash /tmp/jernerics_mystudy_retry_d0.sh" in script

    def test_retry_script_filename_includes_chain_depth(self):
        backend = _make_backend(auto_retry=True)
        backend.container.wrap = lambda cmd, binds: f"wrapped({cmd})"
        script = backend._generate_submit_script(
            self._make_spec(), retry_ctx=self._make_retry_ctx(chain_depth=3)
        )
        assert "/tmp/jernerics_mystudy_retry_d3.sh" in script

    def test_checker_inner_and_wrapper_filenames_stable(self):
        backend = _make_backend(auto_retry=True)
        backend.container.wrap = lambda cmd, binds: f"wrapped({cmd})"
        script = backend._generate_submit_script(
            self._make_spec(), retry_ctx=self._make_retry_ctx()
        )
        assert "/tmp/jernerics_mystudy_checker.sh" in script
        assert "/tmp/jernerics_mystudy_wait_and_check.sh" in script
