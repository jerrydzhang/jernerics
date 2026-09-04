import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from jernerics.backend.adapter import (
    JobResourceSnapshot,
    JobResourcesResult,
    SchedulerAdapter,
)
from jernerics.backend.pueue.adapter import PueueAdapter
from jernerics.backend.slurm.adapter import SlurmAdapter
from jernerics.backend.submission import make_resource_adapter
from jernerics.cli import app
from jernerics.config import ConfigNotFound
from jernerics.post_hook import capture_job_resources
from jernerics.tracking.jsonl_io import scan_events
from jernerics_schema import (
    JobResourceEvent,
    JobSnapshotEvent,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    sweep_id_for,
)
from typer.testing import CliRunner

COMPLETED_ROW = (
    "4242|COMPLETED|0:0|01:02:03|4|16G|512M|256M|98.54%|04:00:00|cpu=4,mem=16G|node01"
)


def _snapshot(job_id, wall_time_s=90.0, state="COMPLETED"):
    return JobResourceSnapshot(
        job_id=job_id,
        state=state,
        exit_code=None,
        wall_time_s=wall_time_s,
        cpu_time_s=None,
        cpu_pct=None,
        max_rss_mb=None,
        ave_rss_mb=None,
        alloc_cpus=None,
        req_mem=None,
        alloc_tres=None,
        node_list=None,
    )


def _sacct_process(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _pueue_host(tasks):
    host = MagicMock()
    host.run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=json.dumps({"tasks": tasks}), stderr=""
    )
    return host


def _done(result="Success", start="2026-09-04T10:00:00+00:00"):
    done = {"result": result, "end": "2026-09-04T10:01:30+00:00"}
    if start is not None:
        done["start"] = start
    return {"Done": done}


def _write_submission_events(tracking_dir, submission_id, backend):
    now = datetime.now(UTC)
    sweep_id = sweep_id_for("proj", "mystudy")
    events = [
        SweepSnapshotEvent(
            event_id=uuid4(),
            recorded_at=now,
            project="proj",
            sweep_id=sweep_id,
            name="mystudy",
            state="running",
        ),
        SubmissionSnapshotEvent(
            event_id=uuid4(),
            recorded_at=now,
            submission_id=submission_id,
            sweep_id=sweep_id,
            backend=backend,
            state=SubmissionState.SUBMITTED,
        ),
        JobSnapshotEvent(
            event_id=uuid4(),
            recorded_at=now,
            job_id=uuid4(),
            submission_id=submission_id,
            scheduler_job_id="mystudy",
            role="trials",
            state=SubmissionState.SUBMITTED,
        ),
    ]
    submission_dir = tracking_dir / "submission"
    submission_dir.mkdir(parents=True)
    (submission_dir / "deploy.jsonl").write_text(
        "".join(event.model_dump_json() + "\n" for event in events)
    )
    return str(submission_id)


def _resource_events(path):
    return [
        event
        for event, _ in scan_events(path, 0)[0]
        if isinstance(event, JobResourceEvent)
    ]


class TestAdapterContract:
    def test_slurm_adapter_satisfies_protocol(self):
        assert isinstance(SlurmAdapter(MagicMock(), remote_dir=""), SchedulerAdapter)

    def test_pueue_adapter_satisfies_protocol(self):
        assert isinstance(
            PueueAdapter(MagicMock(), remote_dir="", cache_dir=""), SchedulerAdapter
        )


class TestMakeResourceAdapter:
    def test_pueue_type_builds_pueue_adapter(self):
        assert isinstance(make_resource_adapter("pueue"), PueueAdapter)

    def test_slurm_type_builds_slurm_adapter(self):
        assert isinstance(make_resource_adapter("slurm"), SlurmAdapter)

    def test_unknown_type_falls_back_to_slurm(self):
        assert isinstance(make_resource_adapter("hpc-grid"), SlurmAdapter)


class TestSlurmAdapterFetch:
    def test_wraps_sacct_snapshot(self):
        adapter = SlurmAdapter(MagicMock(), remote_dir="")
        with patch(
            "jernerics.backend.slurm.sacct.subprocess.run",
            return_value=_sacct_process(COMPLETED_ROW + "\n"),
        ) as run:
            result = adapter.fetch_job_resources("4242")

        assert run.call_args.args[0][:5] == ["sacct", "-n", "-P", "-j", "4242"]
        assert result.error is None
        assert [snapshot.job_id for snapshot in result.snapshots] == ["4242"]
        snapshot = result.snapshots[0]
        assert snapshot.wall_time_s == pytest.approx(3_723.0)
        assert snapshot.max_rss_mb == pytest.approx(512.0)
        assert snapshot.state == "COMPLETED"

    def test_wraps_sacct_failure_as_error_result(self):
        adapter = SlurmAdapter(MagicMock(), remote_dir="")
        with patch(
            "jernerics.backend.slurm.sacct.subprocess.run",
            return_value=_sacct_process(returncode=1, stderr="slurmdbd unreachable"),
        ):
            result = adapter.fetch_job_resources("4242")

        assert result.snapshots == []
        assert result.error is not None
        assert "sacct for job 4242 failed" in result.error


class TestPueueAdapterFetch:
    @staticmethod
    def _adapter(tasks):
        return PueueAdapter(_pueue_host(tasks), remote_dir="", cache_dir="")

    def _tasks(self):
        return {
            "11": {"group": "mystudy", "status": _done()},
            "12": {"group": "mystudy", "status": _done(result="Failed")},
            "13": {"group": "mystudy", "status": {"Running": {"paused": False}}},
            "14": {"group": "other", "status": _done()},
        }

    def test_group_id_resolves_to_per_task_snapshots(self):
        result = self._adapter(self._tasks()).fetch_job_resources("mystudy")

        assert result.error is None
        assert [(s.job_id, s.state) for s in result.snapshots] == [
            ("11", "COMPLETED"),
            ("12", "FAILED"),
        ]

    def test_wall_time_from_start_end_and_resources_none(self):
        result = self._adapter(self._tasks()).fetch_job_resources("mystudy")

        snapshot = result.snapshots[0]

        assert snapshot.wall_time_s == pytest.approx(90.0)
        assert snapshot.cpu_time_s is None
        assert snapshot.cpu_pct is None
        assert snapshot.max_rss_mb is None
        assert snapshot.ave_rss_mb is None
        assert snapshot.exit_code is None

    def test_tasks_without_end_are_skipped(self):
        tasks = {
            "21": {"group": "mystudy", "status": _done()},
            "22": {"group": "mystudy", "status": {"Queued": {}}},
            "23": {"group": "mystudy", "status": {"Running": {"paused": False}}},
        }

        result = self._adapter(tasks).fetch_job_resources("mystudy")

        assert [snapshot.job_id for snapshot in result.snapshots] == ["21"]

    def test_end_without_start_keeps_task_with_null_wall_time(self):
        tasks = {"31": {"group": "mystudy", "status": _done(start=None)}}

        snapshot = self._adapter(tasks).fetch_job_resources("mystudy").snapshots[0]

        assert snapshot.job_id == "31"
        assert snapshot.wall_time_s is None

    def test_malformed_timestamps_are_skipped(self):
        tasks = {"41": {"group": "mystudy", "status": _done(start="not-a-date")}}

        result = self._adapter(tasks).fetch_job_resources("mystudy")

        assert result.snapshots == []
        assert result.error is None

    def test_single_task_id_returns_one_snapshot(self):
        result = self._adapter(self._tasks()).fetch_job_resources("11")

        assert [snapshot.job_id for snapshot in result.snapshots] == ["11"]

    def test_unknown_task_id_is_an_error(self):
        result = self._adapter(self._tasks()).fetch_job_resources("99")

        assert result.snapshots == []
        assert result.error is not None
        assert "99" in result.error

    def test_unknown_group_yields_no_snapshots_and_no_error(self):
        result = self._adapter(self._tasks()).fetch_job_resources("never-submitted")

        assert result.snapshots == []
        assert result.error is None


class TestPostHookDispatch:
    def test_pueue_sweep_captures_without_sacct(self, tmp_path, capsys):
        tracking_dir = tmp_path / "tracking" / "mystudy"
        submission_id = uuid4()
        _write_submission_events(tracking_dir, submission_id, backend="pueue")
        adapter = PueueAdapter(
            _pueue_host(
                {
                    "61": {"group": "mystudy", "status": _done()},
                    "62": {"group": "mystudy", "status": {"Queued": {}}},
                }
            ),
            remote_dir="",
            cache_dir="",
        )
        with (
            patch(
                "jernerics.post_hook.load_backend_config",
                side_effect=ConfigNotFound("no config in post-hook"),
            ),
            patch(
                "jernerics.backend.submission.make_resource_adapter",
                return_value=adapter,
            ) as factory,
            patch("jernerics.backend.slurm.sacct.subprocess.run") as sacct,
            patch("jernerics.post_hook.ship_events_file") as ship,
        ):
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        factory.assert_called_once_with("pueue")
        sacct.assert_not_called()
        ship.assert_called_once()
        assert capsys.readouterr().err == ""
        events = _resource_events(ship.call_args.args[0])
        assert [event.job_id for event in events] == ["61"]
        assert events[0].wall_time_s == pytest.approx(90.0)
        assert events[0].submission_id == str(submission_id)

    def test_job_meta_backend_resolves_pueue(self, tmp_path):
        tracking_dir = tmp_path / "tracking" / "mystudy"
        tracking_dir.mkdir(parents=True)
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "mystudy.json").write_text(
            json.dumps(
                {"job_id": "mystudy", "study_name": "mystudy", "backend": "pueue"}
            )
        )
        adapter = MagicMock()
        with (
            patch(
                "jernerics.post_hook.load_backend_config",
                side_effect=ConfigNotFound("no config in post-hook"),
            ),
            patch(
                "jernerics.backend.submission.make_resource_adapter",
                return_value=adapter,
            ) as factory,
            patch("jernerics.post_hook.ship_events_file"),
        ):
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        factory.assert_called_once_with("pueue")
        adapter.fetch_job_resources.assert_called_once_with("mystudy")

    def test_unknown_backend_defaults_to_slurm_sacct(self, tmp_path):
        tracking_dir = tmp_path / "tracking" / "mystudy"
        _write_submission_events(tracking_dir, uuid4(), backend="slurm")
        with (
            patch(
                "jernerics.post_hook.load_backend_config",
                side_effect=ConfigNotFound("no config in post-hook"),
            ),
            patch(
                "jernerics.backend.slurm.sacct.subprocess.run",
                return_value=_sacct_process(COMPLETED_ROW + "\n"),
            ) as sacct,
            patch("jernerics.post_hook.ship_events_file") as ship,
        ):
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        sacct.assert_called_once()
        ship.assert_called_once()
        events = _resource_events(ship.call_args.args[0])
        assert [event.job_id for event in events] == ["mystudy"]
        assert events[0].wall_time_s == pytest.approx(3_723.0)

    def test_dead_pueue_daemon_logs_one_line_and_ships_nothing(self, tmp_path, capsys):
        tracking_dir = tmp_path / "tracking" / "mystudy"
        _write_submission_events(tracking_dir, uuid4(), backend="pueue")
        host = MagicMock()
        host.run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="daemon not running"
        )
        adapter = PueueAdapter(host, remote_dir="", cache_dir="")
        with (
            patch(
                "jernerics.post_hook.load_backend_config",
                side_effect=ConfigNotFound("no config in post-hook"),
            ),
            patch(
                "jernerics.backend.submission.make_resource_adapter",
                return_value=adapter,
            ),
            patch("jernerics.post_hook.ship_events_file") as ship,
        ):
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        assert "resource capture failed" in capsys.readouterr().err
        ship.assert_not_called()


class TestResourcesCli:
    def test_backend_option_dispatches_through_configured_backend(self):
        backend = MagicMock()
        backend.adapter.fetch_job_resources.return_value = JobResourcesResult(
            [_snapshot("4242")]
        )
        with patch(
            "jernerics.commands.jobs._get_backend",
            return_value=(backend, "", Path(".")),
        ) as get_backend:
            result = CliRunner().invoke(
                app, ["job", "resources", "4242", "--backend", "box"]
            )

        assert result.exit_code == 0
        get_backend.assert_called_once_with("box")
        backend.adapter.fetch_job_resources.assert_called_once_with("4242")
        assert "job_id" in result.output
        assert "4242" in result.output

    def test_without_backend_uses_recorded_job_backend(self):
        factory = MagicMock()
        factory.return_value.fetch_job_resources.return_value = JobResourcesResult(
            [_snapshot("4242")]
        )
        with (
            patch(
                "jernerics.commands.jobs.load_job_backends",
                return_value={"4242": "pueue"},
            ) as load_backends,
            patch("jernerics.commands.jobs.make_resource_adapter", factory),
        ):
            result = CliRunner().invoke(app, ["job", "resources", "4242"])

        assert result.exit_code == 0
        load_backends.assert_called_once()
        factory.assert_called_once_with("pueue")
        assert "4242" in result.output

    def test_missing_data_prints_error_and_succeeds(self):
        adapter = MagicMock()
        adapter.fetch_job_resources.return_value = JobResourcesResult(
            error="pueue has no task 99"
        )
        with (
            patch(
                "jernerics.commands.jobs.load_job_backends",
                return_value={"99": "pueue"},
            ),
            patch(
                "jernerics.commands.jobs.make_resource_adapter",
                return_value=adapter,
            ),
        ):
            result = CliRunner().invoke(app, ["job", "resources", "99"])

        assert result.exit_code == 0
        assert "No accounting data for job 99" in result.output
        assert "pueue has no task 99" in result.output
