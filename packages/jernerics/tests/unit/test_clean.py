from unittest.mock import MagicMock, patch

import pytest
from jernerics.backend.models import JobInfo
from jernerics.backend.slurm_backend import SlurmBackend


def _make_backend(
    jobs: list[JobInfo] | None = None,
    host: MagicMock | None = None,
    remote_dir: str = "$HOME/experiments/test_project",
    cache_dir: str | None = None,
) -> SlurmBackend:
    container = MagicMock()
    container.wrap = lambda cmd, binds: f"apptainer exec ... {cmd}"
    syncer = MagicMock()
    syncer.container_needs_rebuild.return_value = False

    backend = SlurmBackend(
        host=host or MagicMock(),
        container=container,
        syncer=syncer,
        remote_dir=remote_dir,
        cache_dir=cache_dir,
    )

    jobs_list = jobs if jobs is not None else []
    patcher = patch.object(backend, "list_jobs", return_value=jobs_list)
    patcher.start()

    return backend


def _setup_host_calls(
    host: MagicMock,
    *,
    pb_files: str = "",
    dir_exists: bool = True,
    rm_succeeds: bool = True,
) -> None:
    """Configure host.run to respond appropriately to different commands."""

    def run_side_effect(cmd, **kwargs):
        result = MagicMock()
        cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)

        if "find" in cmd_str and ".pb" in cmd_str:
            result.stdout.strip.return_value = pb_files
            result.returncode = 0
            return result
        elif "test -d" in cmd_str:
            result.returncode = 0 if dir_exists else 1
            return result
        elif "rm -rf" in cmd_str:
            result.returncode = 0 if rm_succeeds else 1
            result.stderr = "" if rm_succeeds else "permission denied"
            return result
        elif "mv" in cmd_str or "mkdir" in cmd_str:
            result.returncode = 0
            return result
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

    host.run.side_effect = run_side_effect


class TestCleanDryRun:
    def test_prints_cache_path(self, capsys) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, dir_exists=True)

        backend = _make_backend(host=host)
        backend.clean("test_project", full=False, force=False)

        output = capsys.readouterr().out
        assert "cache" in output
        assert "Dry run" in output

    def test_prints_both_paths_in_full_mode(self, capsys) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, dir_exists=True)

        backend = _make_backend(host=host)
        backend.clean("test_project", full=True, force=False)

        output = capsys.readouterr().out
        assert "cache" in output
        assert "project" in output
        assert "Dry run" in output


class TestCleanActiveJobsBlock:
    def test_blocks_when_jobs_running(self) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"

        backend = _make_backend(
            host=host,
            jobs=[
                JobInfo(job_id="123", name="sweep_test", status="RUNNING"),
            ],
        )

        with pytest.raises(RuntimeError, match="Active jobs"):
            backend.clean("test_project", full=False, force=False)

    def test_allows_when_jobs_completed(self, capsys) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, dir_exists=True)

        backend = _make_backend(
            host=host,
            jobs=[
                JobInfo(job_id="123", name="sweep_test", status="COMPLETED"),
                JobInfo(job_id="124", name="sweep_test", status="FAILED"),
            ],
        )
        backend.clean("test_project", full=False, force=False)

        output = capsys.readouterr().out
        assert "Dry run" in output


class TestCleanUnsyncedPbBlock:
    def test_blocks_when_pb_files_exist(self) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, pb_files="/cache/tracking/study/0.pb")

        backend = _make_backend(host=host)

        with pytest.raises(RuntimeError, match="Unsynced tracking data"):
            backend.clean("test_project", full=False, force=False)

    def test_allows_when_no_pb_files(self, capsys) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, pb_files="", dir_exists=True)

        backend = _make_backend(host=host)
        backend.clean("test_project", full=False, force=False)

        output = capsys.readouterr().out
        assert "Dry run" in output


class TestCleanMissingDirectoryBlock:
    def test_blocks_when_cache_dir_missing(self) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, dir_exists=False)

        backend = _make_backend(host=host)

        with pytest.raises(FileNotFoundError, match="Cache directory"):
            backend.clean("test_project", full=False, force=False)


class TestCleanForceExecution:
    def test_deletes_cache_on_force(self, capsys) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, dir_exists=True, rm_succeeds=True)

        backend = _make_backend(host=host)
        backend.clean("test_project", full=False, force=True)

        output = capsys.readouterr().out
        assert "Deleted" in output
        assert "Dry run" not in output

    def test_deletes_cache_and_project_on_full_force(self, capsys) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, dir_exists=True, rm_succeeds=True)

        backend = _make_backend(host=host, remote_dir="$HOME/experiments/test_project")
        backend.clean("test_project", full=True, force=True)

        output = capsys.readouterr().out
        assert "Deleted" in output
        rm_calls = [
            call for call in host.run.call_args_list if call[0][0][:2] == ["rm", "-rf"]
        ]
        assert len(rm_calls) == 2

    def test_preserves_saved_directory_on_full_force(self, capsys) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.stdout.strip.return_value = ""
            result.returncode = 0
            result.stderr = ""
            return result

        host.run.side_effect = run_side_effect

        backend = _make_backend(host=host, remote_dir="$HOME/experiments/test_project")
        backend.clean("test_project", full=True, force=True)

        output = capsys.readouterr().out
        rm_calls = [
            call for call in host.run.call_args_list if call[0][0][:2] == ["rm", "-rf"]
        ]
        assert len(rm_calls) == 2
        mv_calls = [
            call for call in host.run.call_args_list if call[0][0][:1] == ["mv"]
        ]
        assert len(mv_calls) == 2
        mkdir_calls = [
            call for call in host.run.call_args_list if call[0][0][:1] == ["mkdir"]
        ]
        assert len(mkdir_calls) == 1

    def test_fails_when_rm_errors(self) -> None:
        host = MagicMock()
        host.host = "user@hpc.example.edu"
        _setup_host_calls(host, dir_exists=True, rm_succeeds=False)

        backend = _make_backend(host=host)

        with pytest.raises(RuntimeError, match="Failed to delete"):
            backend.clean("test_project", full=False, force=True)
