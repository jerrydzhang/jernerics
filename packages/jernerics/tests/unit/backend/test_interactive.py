"""Tests for the interactive allocation + reconnectable shell module."""

from unittest.mock import MagicMock

import pytest
from jernerics.backend.slurm.interactive import (
    InteractiveSession,
    InteractiveSessionInfo,
    extract_node,
    format_interactive_script,
    parse_session_lines,
)


def _completed(stdout="", stderr="", returncode=0):
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def _make_session(
    host=None,
    *,
    job_name: str = "jernerics-interactive-proj",
    remote_dir: str = "/home/user/projects/proj",
    container_image: str = "/home/user/projects/proj/container.sif",
    cache_host: str = "/home/user/.cache/jernerics",
    partition: str = "general-gpu",
    time_limit: str = "4:00:00",
    gpus: int = 1,
    mem: str = "32G",
    cpus: int = 8,
    constraint: str | None = None,
    login_target: str | None = "user@login.hpc.edu",
    user: str | None = "user",
    poll_interval: float = 0.0,
) -> InteractiveSession:
    if host is None:
        host = MagicMock()
        host.run.return_value = _completed()
    return InteractiveSession(
        host=host,
        job_name=job_name,
        remote_dir=remote_dir,
        container_image=container_image,
        cache_host=cache_host,
        partition=partition,
        time_limit=time_limit,
        gpus=gpus,
        mem=mem,
        cpus=cpus,
        constraint=constraint,
        login_target=login_target,
        user=user,
        poll_interval=poll_interval,
    )


class TestFormatInteractiveScript:
    def test_basic_script_structure(self):
        script = format_interactive_script(
            job_name="jernerics-interactive-proj",
            partition="general-gpu",
            time_limit="4:00:00",
            mem="32G",
            cpus=8,
            gpus=1,
        )
        assert script.startswith("#!/bin/bash")
        assert "#SBATCH --parsable" in script
        assert "#SBATCH --job-name=jernerics-interactive-proj" in script
        assert "#SBATCH --partition=general-gpu" in script
        assert "#SBATCH --time=4:00:00" in script
        assert "#SBATCH --mem=32G" in script
        assert "#SBATCH --cpus-per-task=8" in script
        assert "#SBATCH --gres=gpu:1" in script
        assert script.rstrip().endswith("sleep infinity")

    def test_gpu_count_in_gres(self):
        script = format_interactive_script(
            job_name="j",
            partition="p",
            time_limit="1:00:00",
            mem="16G",
            cpus=4,
            gpus=4,
        )
        assert "#SBATCH --gres=gpu:4" in script

    def test_constraint_included_when_set(self):
        script = format_interactive_script(
            job_name="j",
            partition="p",
            time_limit="1:00:00",
            mem="16G",
            cpus=4,
            gpus=1,
            constraint="a100",
        )
        assert "#SBATCH --constraint=a100" in script

    def test_no_constraint_directive_when_none(self):
        script = format_interactive_script(
            job_name="j",
            partition="p",
            time_limit="1:00:00",
            mem="16G",
            cpus=4,
            gpus=1,
        )
        assert "constraint" not in script

    def test_rejects_invalid_job_name(self):
        with pytest.raises(ValueError, match="job-name"):
            format_interactive_script(
                job_name="bad name with spaces",
                partition="p",
                time_limit="1:00:00",
                mem="16G",
                cpus=4,
                gpus=1,
            )


class TestExtractNode:
    def test_bare_hostname(self):
        assert extract_node("gpu13") == "gpu13"

    def test_empty_returns_none(self):
        assert extract_node("") is None
        assert extract_node("   ") is None

    def test_none_literal_returns_none(self):
        assert extract_node("None") is None
        assert extract_node("n/a") is None

    def test_multiple_nodes_takes_first(self):
        assert extract_node("gpu13,gpu14") == "gpu13"


class TestParseSessionLines:
    def test_parses_running_session(self):
        sessions = parse_session_lines("12345|RUNNING|gpu13")
        assert len(sessions) == 1
        assert sessions[0] == InteractiveSessionInfo("12345", "RUNNING", "gpu13")

    def test_parses_pending_no_node(self):
        sessions = parse_session_lines("12346|PENDING|")
        assert sessions[0].state == "PENDING"
        assert sessions[0].node is None

    def test_skips_header_row(self):
        sessions = parse_session_lines("JOBID|STATE|NODELIST\n12345|RUNNING|gpu13")
        assert len(sessions) == 1
        assert sessions[0].job_id == "12345"

    def test_empty_output(self):
        assert parse_session_lines("") == []
        assert parse_session_lines("\n") == []

    def test_multiple_lines(self):
        sessions = parse_session_lines("12345|RUNNING|gpu13\n12346|PENDING|")
        assert len(sessions) == 2
        assert sessions[0].node == "gpu13"
        assert sessions[1].node is None


class TestFindExisting:
    def test_returns_none_when_no_jobs(self):
        session = _make_session()
        session.host.run.return_value = _completed(stdout="")
        assert session.find_existing() is None

    def test_returns_running_session(self):
        session = _make_session()
        session.host.run.return_value = _completed(stdout="12345|RUNNING|gpu13")
        info = session.find_existing()
        assert info == InteractiveSessionInfo("12345", "RUNNING", "gpu13")

    def test_returns_pending_session(self):
        session = _make_session()
        session.host.run.return_value = _completed(stdout="12346|PENDING|")
        info = session.find_existing()
        assert info is not None
        assert info.state == "PENDING"
        assert info.node is None

    def test_returns_first_when_multiple(self):
        session = _make_session()
        session.host.run.return_value = _completed(
            stdout="12345|RUNNING|gpu13\n12346|RUNNING|gpu14"
        )
        info = session.find_existing()
        assert info is not None
        assert info.job_id == "12345"


class TestSubmit:
    def test_returns_job_id(self):
        session = _make_session()
        session.host.run.return_value = _completed(stdout="12345\n")
        assert session.submit() == "12345"

    def test_strips_extra_output(self):
        session = _make_session()
        session.host.run.return_value = _completed(
            stdout="12345\nSubmitted batch job\n"
        )
        assert session.submit() == "12345"

    def test_raises_on_failure(self):
        session = _make_session()
        session.host.run.return_value = _completed(
            stdout="", stderr="sbatch: error", returncode=1
        )
        with pytest.raises(RuntimeError, match="Failed to submit interactive job"):
            session.submit()

    def test_raises_when_no_job_id(self):
        session = _make_session()
        session.host.run.return_value = _completed(stdout="\n")
        with pytest.raises(RuntimeError, match="no job id"):
            session.submit()


class TestWaitForRunning:
    def test_returns_node_when_already_running(self):
        session = _make_session(poll_interval=0.0)
        session.host.run.return_value = _completed(stdout="RUNNING|gpu13")
        assert session.wait_for_running("12345") == "gpu13"

    def test_polls_until_running(self):
        session = _make_session(poll_interval=0.0)
        session.host.run.side_effect = [
            _completed(stdout="PENDING|"),
            _completed(stdout="RUNNING|gpu13"),
        ]
        assert session.wait_for_running("12345") == "gpu13"

    def test_raises_on_terminal_state(self):
        session = _make_session(poll_interval=0.0)
        session.host.run.return_value = _completed(stdout="FAILED|")
        with pytest.raises(RuntimeError, match="ended in state FAILED"):
            session.wait_for_running("12345")

    def test_raises_on_timeout(self):
        session = _make_session(poll_interval=0.0)
        session.host.run.return_value = _completed(stdout="PENDING|")
        with pytest.raises(TimeoutError):
            session.wait_for_running("12345", timeout=0.0)


class TestEnd:
    def test_cancels_existing_session(self):
        session = _make_session()
        session.host.run.return_value = _completed(stdout="12345|RUNNING|gpu13")
        ended = session.end()
        assert ended is not None
        assert ended.job_id == "12345"
        # Second host.run call is the scancel.
        cancel_call = session.host.run.call_args_list[-1]
        assert cancel_call.args[0] == ["scancel", "12345"]

    def test_returns_none_when_nothing_to_end(self):
        session = _make_session()
        session.host.run.return_value = _completed(stdout="")
        assert session.end() is None


class TestSshCommand:
    def test_remote_shell_command_runs_apptainer_directly(self):
        session = _make_session()
        cmd = session.remote_shell_command("gpu13")
        assert "cd /home/user/projects/proj" in cmd
        assert "apptainer shell --nv" in cmd
        assert "--pwd /work" in cmd
        assert "--bind /home/user/projects/proj:/work" in cmd
        assert "--bind /home/user/.cache/jernerics:/cache" in cmd
        assert "container.sif" in cmd
        assert "tmux" not in cmd

    def test_ssh_argv_uses_proxyjump(self):
        session = _make_session(login_target="user@login.hpc.edu", user="user")
        argv = session.ssh_argv("gpu13")
        assert argv[0] == "ssh"
        assert "-t" in argv
        assert "ProxyJump=user@login.hpc.edu" in argv
        assert "user@gpu13" in argv

    def test_ssh_argv_without_user_omits_prefix(self):
        session = _make_session(login_target="login.hpc.edu", user=None)
        argv = session.ssh_argv("gpu13")
        assert "gpu13" in argv
        assert "user@gpu13" not in argv

    def test_ssh_argv_requires_login_target(self):
        host = MagicMock()
        host.host = None
        session = _make_session(host=host, login_target=None, user=None)
        with pytest.raises(RuntimeError, match="no login target"):
            session.ssh_argv("gpu13")
