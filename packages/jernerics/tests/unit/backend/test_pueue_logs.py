import json
import time
from unittest.mock import MagicMock

import pytest
from jernerics.backend.backend import Backend
from jernerics.backend.job_meta import save_job_meta
from jernerics.backend.models import (
    JobInfo,
    JobSubmission,
    SubmitResult,
    SweepSubmission,
)
from jernerics.backend.pueue.adapter import PueueAdapter, pueue_group_from_label


def _make_adapter(host=None, **overrides):
    defaults = {
        "remote_dir": "$HOME/projects/proj",
        "cache_dir": "$HOME/.cache/jernerics",
        "parallel": 2,
    }
    defaults.update(overrides)
    if host is None:
        host = MagicMock()
        host.run.return_value = MagicMock(returncode=0, stdout="0")
    return PueueAdapter(host=host, **defaults)


def _host_with(handler):
    host = MagicMock()
    host.run.side_effect = handler
    return host


def _ok(stdout: str):
    return MagicMock(returncode=0, stdout=stdout, stderr="")


def _status_response(tasks: dict):
    return _ok(json.dumps({"tasks": tasks}))


def _log_response(task_id: str, output: str):
    return _ok(json.dumps({task_id: {"output": output}}))


def _queued():
    return {"Queued": {}}


def _running():
    return {"Running": {}}


def _done(result="Success", end="2026-09-04T10:00:00Z"):
    return {"Done": {"result": result, "end": end}}


def _task(group="mystudy", status=None, label=""):
    return {"group": group, "status": status or _queued(), "label": label}


def _issued_commands(host) -> list[list]:
    return [list(call.args[0]) for call in host.run.call_args_list]


def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _s: None)


class TestTaskLogs:
    def test_logs_print_task_output(self, capsys):
        host = _host_with(
            lambda argv, **kw: (
                _log_response("5", "trial output")
                if argv == ["pueue", "log", "5", "--json"]
                else None
            )
        )
        adapter = _make_adapter(host=host)

        adapter.get_logs("5")

        assert "trial output" in capsys.readouterr().out

    def test_logs_honor_stderr_flag(self):
        host = _host_with(
            lambda argv, **kw: (
                _log_response("5", "err output")
                if argv == ["pueue", "log", "5", "--json", "--stderr"]
                else MagicMock(returncode=1, stdout="", stderr="")
            )
        )
        adapter = _make_adapter(host=host)

        adapter.get_logs("5", stderr=True)

        assert ["pueue", "log", "5", "--json", "--stderr"] in _issued_commands(host)

    def test_missing_output_retries_before_erroring(self, monkeypatch):
        _no_sleep(monkeypatch)
        calls = {"log": 0}

        def handler(argv, **kw):
            if argv == ["pueue", "log", "5", "--json"]:
                calls["log"] += 1
                return _log_response("5", "")
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        with pytest.raises(SystemExit):
            adapter.get_logs("5")

        assert calls["log"] == 5

    def test_missing_output_printed_once_available(self, monkeypatch, capsys):
        _no_sleep(monkeypatch)
        attempts = {"n": 0}

        def handler(argv, **kw):
            if argv == ["pueue", "log", "5", "--json"]:
                attempts["n"] += 1
                if attempts["n"] < 3:
                    return _log_response("5", "")
                return _log_response("5", "late output")
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        adapter.get_logs("5")

        assert "late output" in capsys.readouterr().out


class TestFollowTask:
    def test_follow_passes_stderr_flag(self):
        tasks = {"5": _task(status=_done())}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv == ["pueue", "follow", "5", "--stderr"]:
                return MagicMock(returncode=0)
            raise AssertionError(f"unexpected command {argv}")

        host = _host_with(handler)
        adapter = _make_adapter(host=host)

        adapter.get_logs("5", follow=True, stderr=True)

        assert ["pueue", "follow", "5", "--stderr"] in _issued_commands(host)

    def test_follow_prints_final_state_line(self, capsys):
        tasks = {"5": _task(status=_done())}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv == ["pueue", "follow", "5"]:
                return MagicMock(returncode=0)
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        adapter.get_logs("5", follow=True)

        assert "--- job 5 COMPLETED: follow ended ---" in capsys.readouterr().out

    def test_follow_failed_task_reports_failed_state(self, capsys):
        tasks = {"5": _task(status=_done(result="Failed"))}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv == ["pueue", "follow", "5"]:
                return MagicMock(returncode=0)
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        adapter.get_logs("5", follow=True)

        assert "--- job 5 FAILED: follow ended ---" in capsys.readouterr().out


class TestGroupLogs:
    def test_group_logs_print_all_members_in_task_id_order(self, capsys):
        tasks = {
            "7": _task(status=_done()),
            "5": _task(status=_running()),
            "6": _task(),
        }

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv[:2] == ["pueue", "log"]:
                return _log_response(argv[2], f"out{argv[2]}")
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        adapter.get_logs("mystudy")

        out = capsys.readouterr().out
        assert (
            out.index("--- task 5 ---")
            < out.index("out5")
            < out.index("--- task 6 ---")
            < out.index("out6")
            < out.index("--- task 7 ---")
            < out.index("out7")
        )

    def test_group_log_headers_include_labels(self, capsys):
        tasks = {"5": _task(status=_done(), label="mystudy_trial_1")}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv[:2] == ["pueue", "log"]:
                return _log_response("5", "out5")
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        adapter.get_logs("mystudy")

        assert "--- task 5 (mystudy_trial_1) ---" in capsys.readouterr().out

    def test_group_logs_honor_stderr_flag(self, capsys):
        tasks = {"5": _task(status=_done())}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv == ["pueue", "log", "5", "--json", "--stderr"]:
                return _log_response("5", "err output")
            raise AssertionError(f"unexpected command {argv}")

        host = _host_with(handler)
        adapter = _make_adapter(host=host)

        adapter.get_logs("mystudy", stderr=True)

        assert ["pueue", "log", "5", "--json", "--stderr"] in _issued_commands(host)
        assert "err output" in capsys.readouterr().out

    def test_group_logs_retry_then_error_when_all_output_missing(self, monkeypatch):
        _no_sleep(monkeypatch)
        tasks = {"5": _task(status=_done()), "6": _task()}
        calls = {"log": 0}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv[:2] == ["pueue", "log"]:
                calls["log"] += 1
                return _log_response("5", "")
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        with pytest.raises(SystemExit):
            adapter.get_logs("mystudy")

        assert calls["log"] == 10

    def test_group_logs_error_for_unknown_group(self, monkeypatch):
        _no_sleep(monkeypatch)

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response({})
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        with pytest.raises(SystemExit):
            adapter.get_logs("nosuchstudy")


class TestGroupFollow:
    def test_group_follow_follows_first_running_member(self):
        tasks = {
            "5": _task(status=_done()),
            "6": _task(status=_running()),
            "7": _task(status=_running()),
        }

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv == ["pueue", "follow", "6"]:
                return MagicMock(returncode=0)
            raise AssertionError(f"unexpected command {argv}")

        host = _host_with(handler)
        adapter = _make_adapter(host=host)

        adapter.get_logs("mystudy", follow=True)

        assert ["pueue", "follow", "6"] in _issued_commands(host)

    def test_group_follow_follows_last_finished_member_when_none_running(self):
        tasks = {
            "5": _task(status=_done(end="2026-09-04T10:00:00Z")),
            "6": _task(status=_done(end="2026-09-04T11:00:00Z")),
            "7": _task(),
        }

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv == ["pueue", "follow", "6"]:
                return MagicMock(returncode=0)
            raise AssertionError(f"unexpected command {argv}")

        host = _host_with(handler)
        adapter = _make_adapter(host=host)

        adapter.get_logs("mystudy", follow=True)

        assert ["pueue", "follow", "6"] in _issued_commands(host)

    def test_group_follow_defaults_to_first_member_when_all_queued(self):
        tasks = {"6": _task(), "5": _task()}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            if argv == ["pueue", "follow", "5"]:
                return MagicMock(returncode=0)
            raise AssertionError(f"unexpected command {argv}")

        host = _host_with(handler)
        adapter = _make_adapter(host=host)

        adapter.get_logs("mystudy", follow=True)

        assert ["pueue", "follow", "5"] in _issued_commands(host)

    def test_group_follow_unknown_group_errors(self):
        tasks = {"5": _task(group="other", status=_running())}

        def handler(argv, **kw):
            if argv == ["pueue", "status", "--json"]:
                return _status_response(tasks)
            raise AssertionError(f"unexpected command {argv}")

        adapter = _make_adapter(host=_host_with(handler))

        with pytest.raises(SystemExit):
            adapter.get_logs("mystudy", follow=True)


class TestGroupFromLabel:
    @pytest.mark.parametrize(
        "label,expected",
        [
            ("mystudy_setup", "mystudy"),
            ("mystudy_trial_3", "mystudy"),
            ("mystudy_checker", "mystudy"),
            ("my_study_trial_12", "my_study"),
            ("mystudy", None),
            ("unrelated", None),
            ("", None),
        ],
    )
    def test_extracts_group_from_generated_labels(self, label, expected):
        assert pueue_group_from_label(label) == expected


class TestJobMeta:
    def _backend(self, adapter):
        paths = MagicMock()
        paths.remote_dir = "/remote"
        paths.resolve_cache.return_value = "/cache"
        infra = MagicMock()
        infra.adapter = adapter
        infra.paths = paths
        return Backend(host=MagicMock(), infra=infra, syncer=None, project_name="proj")

    def _patch_submit_sweep(self, monkeypatch):
        result = SubmitResult(submissions=[JobSubmission(job_id="mystudy", n_trials=3)])
        monkeypatch.setattr(
            "jernerics.backend.backend.submit_sweep", lambda *a, **k: result
        )

    def _spec(self):
        return SweepSubmission(
            trial_path=None,
            config_path=None,
            study_name="mystudy",
            storage_url="http://tracking",
            n_trials=3,
        )

    def test_sweep_meta_for_pueue_has_no_log_patterns(self, monkeypatch, tmp_path):
        self._patch_submit_sweep(monkeypatch)
        backend = self._backend(
            PueueAdapter(host=MagicMock(), remote_dir="/remote", cache_dir="/cache")
        )

        backend.prepare_and_submit(
            self._spec(),
            project_dir=tmp_path,
            project_name="proj",
            direction="push",
            backend_name="pueue",
            local_cache_dir=tmp_path,
        )

        meta = json.loads((tmp_path / "jobs" / "mystudy.json").read_text())
        assert "output_pattern" not in meta
        assert "error_pattern" not in meta
        assert meta["study_name"] == "mystudy"
        assert meta["backend"] == "pueue"

    def test_sweep_meta_for_other_backends_keeps_patterns(self, monkeypatch, tmp_path):
        self._patch_submit_sweep(monkeypatch)
        backend = self._backend(MagicMock())

        backend.prepare_and_submit(
            self._spec(),
            project_dir=tmp_path,
            project_name="proj",
            direction="push",
            backend_name="slurm",
            local_cache_dir=tmp_path,
        )

        meta = json.loads((tmp_path / "jobs" / "mystudy.json").read_text())
        assert "output_pattern" in meta

    def test_build_meta_for_pueue_has_no_log_patterns(self, tmp_path):
        (tmp_path / "uv.lock").touch()

        class _StubHost:
            def run(self, argv, **kwargs):
                return _ok("New task added with ID 12\n")

        adapter = PueueAdapter(
            host=_StubHost(), remote_dir="/remote", cache_dir="/cache"
        )
        backend = self._backend(adapter)
        backend.infra.paths.resolve_build_dir.return_value = "/cache/build"

        backend.build(
            tmp_path,
            project_name="proj",
            force=True,
            backend_name="pueue",
            local_cache_dir=tmp_path,
        )

        meta = json.loads((tmp_path / "jobs" / "12.json").read_text())
        assert "output_pattern" not in meta
        assert "error_pattern" not in meta
        assert meta["backend"] == "pueue"


class TestListJobsEnrichment:
    def _backend(self, jobs):
        adapter = MagicMock()
        adapter.list_jobs.return_value = jobs
        infra = MagicMock()
        infra.adapter = adapter
        return Backend(host=MagicMock(), infra=infra, syncer=None, project_name="proj")

    def test_pueue_task_jobs_enrich_from_group_meta(self, tmp_path):
        save_job_meta(
            job_id="mystudy",
            study_name="mystudy",
            backend="pueue",
            remote_dir="/remote",
            n_trials=3,
            local_cache_dir=tmp_path,
        )
        backend = self._backend(
            [
                JobInfo(job_id="5", name="mystudy_trial_1", status="RUNNING"),
                JobInfo(job_id="4", name="mystudy_setup", status="COMPLETED"),
                JobInfo(job_id="9", name="", status="QUEUED"),
            ]
        )

        jobs = backend.list_jobs(local_cache_dir=tmp_path)

        assert [job.study_name for job in jobs] == ["mystudy", "mystudy", ""]

    def test_jobs_enrich_by_direct_job_id_match(self, tmp_path):
        save_job_meta(
            job_id="100",
            study_name="slurmstudy",
            remote_dir="/remote",
            n_trials=1,
            local_cache_dir=tmp_path,
        )
        backend = self._backend([JobInfo(job_id="100", name="run", status="RUNNING")])

        jobs = backend.list_jobs(local_cache_dir=tmp_path)

        assert jobs[0].study_name == "slurmstudy"
