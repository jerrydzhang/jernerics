from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from jernerics.sync.mutagen_sync import (
    CONVERGED_STATUS,
    INTERACTIVE_EXCLUDES,
    SESSION_PREFIX,
    MutagenError,
    MutagenNotFound,
    MutagenSync,
    SessionInfo,
    find_mutagen,
    is_converged,
    parse_list_output,
    session_name,
)


def _session(
    name="jernerics-interactive-proj",
    status=CONVERGED_STATUS,
    alpha="/local",
    beta="user@host:/remote",
    alpha_connected=True,
    beta_connected=True,
):
    return SessionInfo(
        name=name,
        status=status,
        alpha_path=alpha,
        beta_path=beta,
        alpha_connected=alpha_connected,
        beta_connected=beta_connected,
    )


def _cp(returncode=0, stdout="", stderr=""):
    return CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestSessionName:
    def test_uses_prefix_and_project(self):
        assert session_name("myproj") == f"{SESSION_PREFIX}-myproj"

    def test_sanitizes_unsafe_characters(self):
        assert session_name("my_proj 2.0!") == f"{SESSION_PREFIX}-my_proj-2.0"

    def test_falls_back_for_empty(self):
        assert session_name("!!!") == f"{SESSION_PREFIX}-project"

    def test_starts_with_prefix(self):
        assert session_name("anything").startswith(SESSION_PREFIX + "-")


class TestParseListOutput:
    def test_empty_output(self):
        assert parse_list_output("") == []

    def test_single_session(self):
        out = "jernerics-interactive-p\tWatching\t/local\tuser@h:/r\ttrue\ttrue\n"
        sessions = parse_list_output(out)
        assert len(sessions) == 1
        s = sessions[0]
        assert s.name == "jernerics-interactive-p"
        assert s.status == "Watching"
        assert s.alpha_path == "/local"
        assert s.beta_path == "user@h:/r"
        assert s.alpha_connected is True
        assert s.beta_connected is True

    def test_multiple_sessions(self):
        out = "a\tWatching\t/l1\t/l2\ttrue\ttrue\nb\tSaving\t/l3\t/l4\ttrue\tfalse\n"
        sessions = parse_list_output(out)
        assert [s.name for s in sessions] == ["a", "b"]
        assert sessions[1].beta_connected is False

    def test_malformed_lines_skipped(self):
        out = "bad\tline\njernerics-interactive-p\tWatching\t/a\t/b\ttrue\ttrue\n"
        sessions = parse_list_output(out)
        assert len(sessions) == 1
        assert sessions[0].name == "jernerics-interactive-p"

    def test_connected_false(self):
        out = "n\tDisconnected\t/a\t/b\tfalse\tfalse\n"
        s = parse_list_output(out)[0]
        assert s.alpha_connected is False
        assert s.beta_connected is False


class TestIsConverged:
    def test_watching_and_connected(self):
        assert is_converged(_session(status="Watching")) is True

    def test_saving_not_converged(self):
        assert is_converged(_session(status="Saving")) is False

    def test_conflict_not_converged(self):
        assert is_converged(_session(status="Conflict resolution required")) is False

    def test_alpha_disconnected(self):
        assert is_converged(_session(alpha_connected=False)) is False

    def test_beta_disconnected(self):
        assert is_converged(_session(beta_connected=False)) is False


class TestBuildCreateCommand:
    def test_structure_positionals_and_flags(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(
            "/local", "user@host", "/remote", name="jernerics-interactive-p"
        )
        assert cmd[0:3] == ["sync", "create", "/local"]
        assert cmd[3] == "user@host:/remote"
        assert "--name" in cmd
        assert cmd[cmd.index("--name") + 1] == "jernerics-interactive-p"

    def test_beta_endpoint_joins_host_and_dir(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(
            "/l", "jerry@hpc.storrs.hpc.uconn.edu", "~/exp/proj", name="n"
        )
        assert "jerry@hpc.storrs.hpc.uconn.edu:~/exp/proj" in cmd

    def test_mode_and_watch_mode(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command("/l", "h", "/r", name="n")
        assert cmd[cmd.index("--mode") + 1] == "two-way-safe"
        assert cmd[cmd.index("--watch-mode") + 1] == "portable"

    def test_defaults_include_ignore_vcs(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command("/l", "h", "/r", name="n")
        assert "--ignore-vcs" in cmd

    def test_ignore_vcs_can_be_disabled(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command("/l", "h", "/r", name="n", ignore_vcs=False)
        assert "--ignore-vcs" not in cmd

    def test_excludes_emitted_as_repeatable_i(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(
            "/l", "h", "/r", name="n", excludes=["results/", "*.pyc"]
        )
        # each pattern preceded by -i
        for pat in ["results/", "*.pyc"]:
            assert "-i" in cmd
            assert pat in cmd
        assert cmd.count("-i") == 2

    def test_default_excludes_used_when_omitted(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command("/l", "h", "/r", name="n")
        assert cmd.count("-i") == len(INTERACTIVE_EXCLUDES)
        for pat in INTERACTIVE_EXCLUDES:
            assert pat in cmd

    def test_path_object_accepted(self):
        from pathlib import Path

        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(Path("/local"), "h", "/r", name="n")
        assert "/local" in cmd


class TestStart:
    def _run_dispatch(self, converged_status="Watching"):
        state = {"name": None}

        def fake_run(cmd, **kwargs):
            sub = cmd[2]
            if sub == "create":
                if "--name" in cmd:
                    state["name"] = cmd[cmd.index("--name") + 1]
                return _cp(0)
            if sub == "list":
                name = state["name"] or "jernerics-interactive-p"
                out = f"{name}\t{converged_status}\t/l\t/h:r\ttrue\ttrue\n"
                return _cp(0, stdout=out)
            return _cp(0)

        return fake_run

    def test_success_returns_name(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            side_effect=self._run_dispatch(),
        ):
            name = sync.start(
                "/local", "user@host", "/remote", name="jernerics-interactive-p"
            )
        assert name == "jernerics-interactive-p"

    def test_create_failure_raises(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)

        def fake_run(cmd, **kwargs):
            if cmd[2] == "create":
                return _cp(1, stderr="boom")
            return _cp(0, stdout="")

        with (
            patch("jernerics.sync.mutagen_sync.subprocess.run", side_effect=fake_run),
            pytest.raises(MutagenError, match="sync create failed"),
        ):
            sync.start("/l", "h", "/r", name="n", convergence_timeout=1)

    def test_convergence_timeout_raises(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)

        def fake_run(cmd, **kwargs):
            if cmd[2] == "create":
                return _cp(0)
            return _cp(0, stdout="n\tSaving\t/l\t/h:r\ttrue\tfalse\n")

        with (
            patch("jernerics.sync.mutagen_sync.subprocess.run", side_effect=fake_run),
            pytest.raises(MutagenError, match="did not converge"),
        ):
            sync.start("/l", "h", "/r", name="n", convergence_timeout=1)


class TestWaitConverged:
    def test_converges(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with patch.object(MutagenSync, "list_sessions", return_value=[_session()]):
            assert sync.wait_converged("jernerics-interactive-proj", timeout=5) is True

    def test_times_out_when_not_converged(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with patch.object(
            MutagenSync, "list_sessions", return_value=[_session(status="Saving")]
        ):
            assert sync.wait_converged("jernerics-interactive-proj", timeout=1) is False

    def test_raises_when_session_never_appears(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with (
            patch.object(MutagenSync, "list_sessions", return_value=[]),
            pytest.raises(MutagenError, match="not found"),
        ):
            sync.wait_converged("ghost", timeout=1)


class TestTerminate:
    def test_success(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run", return_value=_cp(0)
        ) as mock_run:
            sync.terminate("jernerics-interactive-p")
        args = mock_run.call_args[0][0]
        assert args[1:4] == ["sync", "terminate", "jernerics-interactive-p"]

    def test_idempotent_when_already_gone(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        gone = _cp(1, stderr='specification "x" did not match any sessions')
        with patch("jernerics.sync.mutagen_sync.subprocess.run", return_value=gone):
            sync.terminate("jernerics-interactive-p")  # must not raise

    def test_real_failure_raises(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        with (
            patch(
                "jernerics.sync.mutagen_sync.subprocess.run",
                return_value=_cp(1, stderr="daemon exploded"),
            ),
            pytest.raises(MutagenError, match="terminate"),
        ):
            sync.terminate("jernerics-interactive-p")


class TestListSessions:
    def test_parses_all_sessions(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        out = (
            "jernerics-interactive-a\tWatching\t/la\t/ra\ttrue\ttrue\n"
            "other\tSaving\t/lb\t/rb\ttrue\tfalse\n"
        )
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            return_value=_cp(0, stdout=out),
        ):
            sessions = sync.list_sessions()
        assert [s.name for s in sessions] == ["jernerics-interactive-a", "other"]

    def test_client_side_name_filter(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        out = (
            "jernerics-interactive-a\tWatching\t/la\t/ra\ttrue\ttrue\n"
            "jernerics-interactive-b\tSaving\t/lb\t/rb\ttrue\ttrue\n"
        )
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            return_value=_cp(0, stdout=out),
        ):
            sessions = sync.list_sessions("jernerics-interactive-a")
        assert [s.name for s in sessions] == ["jernerics-interactive-a"]

    def test_filter_missing_yields_empty_not_error(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run", return_value=_cp(0, stdout="")
        ):
            assert sync.list_sessions("nope") == []

    def test_list_failure_raises(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        with (
            patch(
                "jernerics.sync.mutagen_sync.subprocess.run",
                return_value=_cp(1, stderr="daemon down"),
            ),
            pytest.raises(MutagenError, match="sync list failed"),
        ):
            sync.list_sessions()


class TestOrphans:
    def test_find_orphans_excludes_alive(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        sessions = [
            _session(name="jernerics-interactive-alive"),
            _session(name="jernerics-interactive-stale", status="Connecting"),
            _session(name="unrelated-tool", status="Watching"),
        ]
        with patch.object(MutagenSync, "list_sessions", return_value=sessions):
            orphans = sync.find_orphans(["jernerics-interactive-alive"])
        assert [o.name for o in orphans] == ["jernerics-interactive-stale"]

    def test_find_orphans_all_when_no_alive(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        sessions = [_session(name="jernerics-interactive-a")]
        with patch.object(MutagenSync, "list_sessions", return_value=sessions):
            orphans = sync.find_orphans()
        assert [o.name for o in orphans] == ["jernerics-interactive-a"]

    def test_terminate_orphans_removes_them(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        sessions = [
            _session(name="jernerics-interactive-stale"),
            _session(name="jernerics-interactive-keep"),
        ]
        with (
            patch.object(MutagenSync, "list_sessions", return_value=sessions),
            patch.object(MutagenSync, "terminate") as mock_term,
        ):
            removed = sync.terminate_orphans(["jernerics-interactive-keep"])
        assert removed == ["jernerics-interactive-stale"]
        mock_term.assert_called_once_with("jernerics-interactive-stale")


class TestAvailability:
    def test_available_true(self):
        with patch("shutil.which", return_value="/usr/bin/mutagen"):
            assert MutagenSync.available() is True
            assert find_mutagen() == "/usr/bin/mutagen"

    def test_available_false(self):
        with patch("shutil.which", return_value=None):
            assert MutagenSync.available() is False
            with pytest.raises(MutagenNotFound):
                find_mutagen()

    def test_methods_raise_when_absent(self):
        sync = MutagenSync()  # no explicit path; mutagen "missing"
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(MutagenNotFound),
        ):
            sync.list_sessions()
