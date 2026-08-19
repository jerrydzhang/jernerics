from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from jernerics.sync.exclusions import (
    BUILTIN_EXCLUDES,
    IGNORE_FILENAME,
    mutagen_ignores,
    project_excludes,
)
from jernerics.sync.mutagen_sync import (
    CONVERGED_STATUS,
    SESSION_PREFIX,
    MutagenError,
    MutagenNotFound,
    MutagenSync,
    SessionInfo,
    find_mutagen,
    is_converged,
    is_idle,
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
    conflicts=0,
):
    return SessionInfo(
        name=name,
        status=status,
        alpha_path=alpha,
        beta_path=beta,
        alpha_connected=alpha_connected,
        beta_connected=beta_connected,
        conflicts=conflicts,
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
        out = "jernerics-interactive-p\tWatching\t/local\tuser@h:/r\ttrue\ttrue\t0\n"
        sessions = parse_list_output(out)
        assert len(sessions) == 1
        s = sessions[0]
        assert s.name == "jernerics-interactive-p"
        assert s.status == "Watching"
        assert s.alpha_path == "/local"
        assert s.beta_path == "user@h:/r"
        assert s.alpha_connected is True
        assert s.beta_connected is True
        assert s.conflicts == 0

    def test_conflict_count_parsed(self):
        out = "n\tWatching\t/a\t/b\ttrue\ttrue\t2\n"
        assert parse_list_output(out)[0].conflicts == 2

    def test_multiple_sessions(self):
        out = (
            "a\tWatching\t/l1\t/l2\ttrue\ttrue\t0\n"
            "b\tSaving\t/l3\t/l4\ttrue\tfalse\t2\n"
        )
        sessions = parse_list_output(out)
        assert [s.name for s in sessions] == ["a", "b"]
        assert sessions[1].beta_connected is False
        assert sessions[1].conflicts == 2

    def test_malformed_lines_skipped(self):
        out = (
            "bad\tline\n"
            "old\tWatching\t/a\t/b\ttrue\ttrue\n"
            "jernerics-interactive-p\tWatching\t/a\t/b\ttrue\ttrue\t0\n"
        )
        sessions = parse_list_output(out)
        assert len(sessions) == 1
        assert sessions[0].name == "jernerics-interactive-p"

    def test_connected_false(self):
        out = "n\tDisconnected\t/a\t/b\tfalse\tfalse\t0\n"
        s = parse_list_output(out)[0]
        assert s.alpha_connected is False
        assert s.beta_connected is False


class TestIsIdle:
    def test_watching_and_connected(self):
        assert is_idle(_session(status="Watching")) is True

    def test_conflicts_do_not_block_idle(self):
        assert is_idle(_session(conflicts=2)) is True

    def test_saving_not_idle(self):
        assert is_idle(_session(status="Saving")) is False

    def test_conflict_status_not_idle(self):
        assert is_idle(_session(status="Conflict resolution required")) is False

    def test_alpha_disconnected(self):
        assert is_idle(_session(alpha_connected=False)) is False

    def test_beta_disconnected(self):
        assert is_idle(_session(beta_connected=False)) is False


class TestIsConverged:
    def test_idle_without_conflicts(self):
        assert is_converged(_session()) is True

    def test_conflicts_block_convergence_despite_idle(self):
        session = _session(conflicts=2)
        assert is_idle(session) is True
        assert is_converged(session) is False

    def test_saving_not_converged(self):
        assert is_converged(_session(status="Saving")) is False

    def test_conflict_status_not_converged(self):
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

    def test_default_excludes_are_builtin_minus_vcs_patterns(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command("/l", "h", "/r", name="n")
        i_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-i"]
        assert i_args == mutagen_ignores(BUILTIN_EXCLUDES)
        assert ".git/" not in i_args

    def test_default_excludes_derived_from_local_dir(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("scratch/\n")
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(tmp_path, "h", "/r", name="n")
        i_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-i"]
        assert i_args == mutagen_ignores(project_excludes(tmp_path))
        assert "scratch/" in i_args

    def test_ignore_vcs_drops_vcs_pattern_entries(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(
            "/l", "h", "/r", name="n", excludes=[".git/", "results/"]
        )
        i_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-i"]
        assert i_args == ["results/"]

    def test_no_ignore_vcs_keeps_vcs_pattern_entries(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(
            "/l",
            "h",
            "/r",
            name="n",
            excludes=[".git/", "results/"],
            ignore_vcs=False,
        )
        i_args = [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-i"]
        assert i_args == [".git/", "results/"]

    def test_path_object_accepted(self):
        from pathlib import Path

        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(Path("/local"), "h", "/r", name="n")
        assert "/local" in cmd


class TestStart:
    def _run_dispatch(self, converged_status="Watching", conflicts="0"):
        state = {"name": None}

        def fake_run(cmd, **kwargs):
            sub = cmd[2]
            if sub == "create":
                if "--name" in cmd:
                    state["name"] = cmd[cmd.index("--name") + 1]
                return _cp(0)
            if sub == "list":
                name = state["name"] or "jernerics-interactive-p"
                out = f"{name}\t{converged_status}\t/l\t/h:r\ttrue\ttrue\t{conflicts}\n"
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
            return _cp(0, stdout="n\tSaving\t/l\t/h:r\ttrue\tfalse\t0\n")

        with (
            patch("jernerics.sync.mutagen_sync.subprocess.run", side_effect=fake_run),
            pytest.raises(MutagenError, match="did not reach idle"),
        ):
            sync.start("/l", "h", "/r", name="n", convergence_timeout=1)

    def test_start_succeeds_with_conflicts(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            side_effect=self._run_dispatch(conflicts="2"),
        ):
            name = sync.start("/l", "h", "/r", name="n", convergence_timeout=5)
        assert name == "n"


class TestWaitIdle:
    def test_returns_true_when_idle(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with patch.object(MutagenSync, "list_sessions", return_value=[_session()]):
            assert sync.wait_idle("jernerics-interactive-proj", timeout=5) is True

    def test_returns_true_when_idle_with_conflicts(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with patch.object(
            MutagenSync, "list_sessions", return_value=[_session(conflicts=2)]
        ):
            assert sync.wait_idle("jernerics-interactive-proj", timeout=5) is True

    def test_times_out_when_not_idle(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with patch.object(
            MutagenSync, "list_sessions", return_value=[_session(status="Saving")]
        ):
            assert sync.wait_idle("jernerics-interactive-proj", timeout=1) is False

    def test_raises_when_session_never_appears(self):
        sync = MutagenSync(mutagen_path="/p/mutagen", poll_interval=0.01)
        with (
            patch.object(MutagenSync, "list_sessions", return_value=[]),
            pytest.raises(MutagenError, match="not found"),
        ):
            sync.wait_idle("ghost", timeout=1)


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
            "jernerics-interactive-a\tWatching\t/la\t/ra\ttrue\ttrue\t0\n"
            "other\tSaving\t/lb\t/rb\ttrue\tfalse\t0\n"
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
            "jernerics-interactive-a\tWatching\t/la\t/ra\ttrue\ttrue\t0\n"
            "jernerics-interactive-b\tSaving\t/lb\t/rb\ttrue\ttrue\t2\n"
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


class TestConflictedPaths:
    def test_parses_paths_for_named_session(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        out = "jernerics-interactive-p\tsrc/a.py\tsrc/b.py\t\nother\tsrc/x.py\t\n"
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            return_value=_cp(0, stdout=out),
        ) as mock_run:
            paths = sync.conflicted_paths("jernerics-interactive-p")
        assert paths == ["src/a.py", "src/b.py"]
        args = mock_run.call_args[0][0]
        assert args[1:3] == ["sync", "list"]
        assert "--template" in args

    def test_empty_conflicts_yield_empty_list(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        out = "jernerics-interactive-p\t\n"
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            return_value=_cp(0, stdout=out),
        ):
            assert sync.conflicted_paths("jernerics-interactive-p") == []

    def test_absent_session_yields_empty_list(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        out = "other\tsrc/x.py\t\n"
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            return_value=_cp(0, stdout=out),
        ):
            assert sync.conflicted_paths("jernerics-interactive-p") == []

    def test_root_template_falls_back_to_legacy_path_field(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        template_error = (
            "unable to execute formatting template: can't evaluate field Root"
        )
        legacy_out = "jernerics-interactive-p\tsrc/a.py\t\n"
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            side_effect=[
                _cp(1, stderr=template_error),
                _cp(0, stdout=legacy_out),
            ],
        ) as mock_run:
            paths = sync.conflicted_paths("jernerics-interactive-p")
        assert paths == ["src/a.py"]
        legacy_template = mock_run.call_args[0][0][4]
        assert ".Root" not in legacy_template
        assert ".Path" in legacy_template

    def test_other_template_errors_still_raise(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        with (
            patch(
                "jernerics.sync.mutagen_sync.subprocess.run",
                return_value=_cp(1, stderr="some other failure"),
            ),
            pytest.raises(MutagenError, match="sync list failed"),
        ):
            sync.conflicted_paths("jernerics-interactive-p")


class TestFlush:
    def test_success_runs_flush_for_named_session(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        with patch(
            "jernerics.sync.mutagen_sync.subprocess.run",
            return_value=_cp(0, stdout=""),
        ) as mock_run:
            sync.flush("jernerics-interactive-p")
        args = mock_run.call_args[0][0]
        assert args[1:4] == ["sync", "flush", "jernerics-interactive-p"]
        assert "--skip-wait" not in args

    def test_failure_raises_mutagen_error(self):
        sync = MutagenSync(mutagen_path="/p/mutagen")
        with (
            patch(
                "jernerics.sync.mutagen_sync.subprocess.run",
                return_value=_cp(1, stderr="no such session"),
            ),
            pytest.raises(MutagenError, match="sync flush"),
        ):
            sync.flush("jernerics-interactive-p")


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
