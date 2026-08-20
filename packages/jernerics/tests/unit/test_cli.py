import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from jernerics.commands.project import (
    _create_minimal_pyproject,
    _get_default_jernerics_config,
)
from jernerics.sync.mutagen_sync import SessionInfo
from jernerics.tracking.batch_sync import ReplayResult
from jernerics.tracking.jsonl_io import TrackingWriter
from jernerics_schema import ValueEvent


class TestGetDefaultJernericsConfig:
    def test_returns_dict_with_backends(self):
        config = _get_default_jernerics_config("myproject")

        assert "backends" in config
        assert "hpc" in config["backends"]

    def test_hpc_backend_structure(self):
        config = _get_default_jernerics_config("myproject")
        hpc = config["backends"]["hpc"]

        assert hpc["type"] == "slurm"
        assert "host" in hpc
        assert "myproject" in hpc["remote_dir"]
        assert "slurm" in hpc
        assert hpc["slurm"]["partition"] == "priority"
        assert hpc["slurm"]["time"] == "1:00:00"
        assert hpc["slurm"]["mem"] == "16G"
        assert hpc["slurm"]["cpus"] == 4

    def test_uses_project_name_in_remote_dir(self):
        config = _get_default_jernerics_config("test-project-123")
        hpc = config["backends"]["hpc"]

        assert "test-project-123" in hpc["remote_dir"]


class TestCreateMinimalPyproject:
    def test_returns_dict_with_required_keys(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert "project" in pyproject
        assert "tool" in pyproject
        assert "build-system" in pyproject

    def test_project_section_structure(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert pyproject["project"]["name"] == "myproject"
        assert "version" in pyproject["project"]
        assert "requires-python" in pyproject["project"]
        assert "jernerics" in pyproject["project"]["dependencies"]

    def test_tool_section_structure(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert "uv" in pyproject["tool"]
        assert "jernerics" in pyproject["tool"]
        assert "sources" in pyproject["tool"]["uv"]
        assert "jernerics" in pyproject["tool"]["uv"]["sources"]

    def test_build_system_structure(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert "requires" in pyproject["build-system"]
        assert "build-backend" in pyproject["build-system"]

    def test_backends_in_config(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert "backends" in pyproject["tool"]["jernerics"]
        assert "hpc" in pyproject["tool"]["jernerics"]["backends"]


class TestInitCommand:
    def test_init_creates_pyproject(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.commands.project.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.commands.project import init

                init(str(project_dir))

        assert (project_dir / "pyproject.toml").exists()

    def test_init_creates_container_def(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.commands.project.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.commands.project import init

                init(str(project_dir))

        assert (project_dir / "container.def").exists()

    def test_init_scaffolds_trial_and_config(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.commands.project.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.commands.project import init

                init(str(project_dir))

        trial = (project_dir / "trial.py").read_text()
        assert "trial_config" in trial
        assert "trial_tracker" in trial
        assert "tracker.finish" in trial
        assert (project_dir / "config.py").exists()

    def test_init_preserves_existing_trial_and_config(self, tmp_path):
        project_dir = tmp_path / "existing-project"
        project_dir.mkdir()
        (project_dir / "trial.py").write_text("# my trial")
        (project_dir / "config.py").write_text("# my config")

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.commands.project.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.commands.project import init

                init(str(project_dir))

        assert (project_dir / "trial.py").read_text() == "# my trial"
        assert (project_dir / "config.py").read_text() == "# my config"

    def test_init_requires_uv(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None

            from jernerics.commands.project import init

            with pytest.raises(SystemExit):
                init(str(project_dir))

    def test_init_invalid_starter(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"

            from jernerics.commands.project import init

            with pytest.raises(SystemExit):
                init(str(project_dir), starter="nonexistent")

    def test_init_preserves_existing_container_def(self, tmp_path):
        project_dir = tmp_path / "existing-project"
        project_dir.mkdir()
        (project_dir / "container.def").write_text("existing definition")

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.commands.project.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.commands.project import init

                init(str(project_dir))

        assert (project_dir / "container.def").read_text() == "existing definition"


class TestMainFunction:
    def test_main_calls_app(self):
        from jernerics.cli import main

        with patch("jernerics.cli.app") as mock_app:
            main()
            mock_app.assert_called_once()


class TestCommandTree:
    def test_root_exposes_locked_commands_and_groups(self):
        from jernerics.cli import app

        assert [c.name for c in app.registered_commands] == ["init", "local", "run"]
        assert [g.name for g in app.registered_groups] == [
            "interactive",
            "job",
            "backend",
            "tracking",
        ]

    def test_groups_expose_locked_subcommands(self):
        from jernerics.cli import app

        expected = {
            "interactive": ["start", "stop"],
            "job": ["list", "cancel", "logs", "wait"],
            "backend": ["build", "clean"],
            "tracking": ["replay", "runs", "summary", "diff", "trace"],
        }
        actual = {}
        for info in app.registered_groups:
            instance = info.typer_instance
            assert instance is not None
            actual[info.name] = [c.name for c in instance.registered_commands]

        assert actual == expected

    def test_interactive_sync_group_exposes_status_and_resolve(self):
        from jernerics.cli import app

        interactive = next(
            g.typer_instance for g in app.registered_groups if g.name == "interactive"
        )
        assert interactive is not None
        assert [g.name for g in interactive.registered_groups] == ["sync"]
        sync = interactive.registered_groups[0].typer_instance
        assert sync is not None
        assert [c.name for c in sync.registered_commands] == ["status", "resolve"]


class TestWaitCommand:
    def test_wait_calls_backend_with_correct_args(self, capsys):
        from jernerics.commands.jobs import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.return_value = True

        with patch(
            "jernerics.commands.jobs._get_backend",
            return_value=(mock_backend, "proj", Path("/tmp")),
        ):
            wait("job-123", backend_name="hpc", timeout=None, poll_interval=10)

        mock_backend.wait_for_completion.assert_called_once_with(
            "job-123", poll_interval=10, timeout=None
        )
        out = capsys.readouterr().out
        assert "completed successfully" in out

    def test_wait_exit_zero_on_success(self, capsys):
        from jernerics.commands.jobs import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.return_value = True

        with patch(
            "jernerics.commands.jobs._get_backend",
            return_value=(mock_backend, "proj", Path("/tmp")),
        ):
            wait("job-123", backend_name="hpc", timeout=None, poll_interval=10)

        out = capsys.readouterr().out
        assert "Job job-123 completed successfully" in out

    def test_wait_exit_one_on_failure(self, capsys):
        from jernerics.commands.jobs import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.return_value = False

        with (
            patch(
                "jernerics.commands.jobs._get_backend",
                return_value=(mock_backend, "proj", Path("/tmp")),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            wait("job-123", backend_name="hpc", timeout=None, poll_interval=10)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "finished with non-success status" in out

    def test_wait_exit_two_on_timeout(self, capsys):
        from jernerics.commands.jobs import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.side_effect = TimeoutError("timed out")

        with (
            patch(
                "jernerics.commands.jobs._get_backend",
                return_value=(mock_backend, "proj", Path("/tmp")),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            wait("job-123", backend_name="hpc", timeout=30, poll_interval=5)

        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        assert "still running after 30s" in out
        mock_backend.wait_for_completion.assert_called_once_with(
            "job-123", poll_interval=5, timeout=30
        )


class TestRunsCommand:
    def test_runs_json_outputs_analysis(self, capsys):
        from jernerics.commands.tracking import runs

        canned = [{"study_name": "s", "trial_id": 0, "status": "running"}]
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.get_all_runs", return_value=canned),
        ):
            runs(json_output=True)

        assert json.loads(capsys.readouterr().out) == canned

    def test_runs_text_renders_without_error(self, capsys):
        from jernerics.commands.tracking import runs

        canned = [
            {
                "study_name": "s",
                "trial_id": 0,
                "label": "s",
                "status": "completed",
                "min_step": 0,
                "max_step": 9,
                "duration_s": 12.0,
                "created_ns": None,
                "params": {"lr": 0.1},
                "priority_key": "loss",
                "priority_value": 0.5,
            }
        ]
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.get_all_runs", return_value=canned),
        ):
            runs(json_output=False)

        out = capsys.readouterr().out
        assert "s" in out
        assert "completed" in out

    def test_runs_empty_message(self, capsys):
        from jernerics.commands.tracking import runs

        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.get_all_runs", return_value=[]),
        ):
            runs(json_output=False)

        assert "No runs found" in capsys.readouterr().out


class TestSummaryCommand:
    def test_summary_json_outputs_analysis(self, capsys):
        from jernerics.commands.tracking import summary

        canned = {
            "study_name": "s",
            "metrics": {},
            "params": {},
            "artifacts": [],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=True)

        assert json.loads(capsys.readouterr().out) == canned

    def test_summary_missing_run_exits(self, capsys):
        from jernerics.commands.tracking import summary

        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            summary("ghost", json_output=False)

        assert exc_info.value.code == 1

    def test_summary_text_renders_git_hash(self, capsys):
        from jernerics.commands.tracking import summary

        canned = {
            "study_name": "s",
            "trial_id": 0,
            "label": "s",
            "status": "completed",
            "min_step": 0,
            "max_step": 4999,
            "duration_s": 239.1,
            "params": {},
            "metrics": {},
            "artifacts": [],
            "git_hash": "4a5e1097211230273aa1888d482bab2885990fb5",
            "text_metrics": [],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=False)

        out = capsys.readouterr().out
        assert "git: 4a5e109" in out

    def test_summary_text_renders_text_metrics_section(self, capsys):
        from jernerics.commands.tracking import summary

        canned = {
            "study_name": "s",
            "trial_id": 0,
            "label": "s",
            "status": "running",
            "min_step": 0,
            "max_step": 9,
            "duration_s": None,
            "params": {},
            "metrics": {},
            "artifacts": [],
            "git_hash": None,
            "text_metrics": [{"key": "pred_expr", "n_points": 5}],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=False)

        out = capsys.readouterr().out
        assert "Text metrics:" in out
        assert "pred_expr (5 points)" in out

    def test_summary_text_omits_text_metrics_when_absent(self, capsys):
        from jernerics.commands.tracking import summary

        canned = {
            "study_name": "s",
            "trial_id": 0,
            "label": "s",
            "status": "running",
            "min_step": 0,
            "max_step": 9,
            "duration_s": None,
            "params": {},
            "metrics": {},
            "artifacts": [],
            "git_hash": None,
            "text_metrics": [],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=False)

        out = capsys.readouterr().out
        assert "Text metrics:" not in out


class TestDiffCommand:
    def test_diff_json_outputs_analysis(self, capsys):
        from jernerics.commands.tracking import diff

        canned = {
            "run_a": {"label": "a"},
            "run_b": {"label": "b"},
            "param_diff": [],
            "param_match_count": 0,
            "param_match": [],
            "metric_diff": [],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_run_diff", return_value=canned),
        ):
            diff("a", "b", json_output=True)

        assert json.loads(capsys.readouterr().out) == canned

    def test_diff_missing_run_exits(self, capsys):
        from jernerics.commands.tracking import diff

        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            diff("a", "b", json_output=False)

        assert exc_info.value.code == 1


def _value_event(seq: int) -> ValueEvent:
    return ValueEvent(
        event_id=uuid4(),
        recorded_at=datetime.now(timezone.utc),
        trial_id=uuid4(),
        key="loss",
        step=seq,
        value=0.5,
    )


def _write_jsonl(path: Path, n: int, study: str = "sweep1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TrackingWriter(path) as writer:
        for i in range(n):
            writer.write_event(_value_event(i))


class TestReplayCommand:
    def test_passes_resolved_server_and_dir_to_replay(self):
        from jernerics.commands.tracking import replay

        with patch(
            "jernerics.commands.tracking.replay_tracking", return_value=ReplayResult()
        ) as mock_rt:
            replay(server="http://srv:8000", tracking_dir=Path("/tmp/trk"))

        kwargs = mock_rt.call_args.kwargs
        assert kwargs["base_url"] == "http://srv:8000"
        assert kwargs["tracking_dir"] == Path("/tmp/trk")
        assert kwargs["study"] is None

    def test_resolves_tracking_dir_from_cache_when_unset(self, tmp_path):
        from jernerics.commands.tracking import replay

        with (
            patch(
                "jernerics.commands.tracking.replay_tracking",
                return_value=ReplayResult(),
            ) as mock_rt,
            patch("jernerics.commands.tracking.cache_dir", return_value=tmp_path),
        ):
            replay(server="http://srv:8000")

        assert mock_rt.call_args.kwargs["tracking_dir"] == tmp_path / "tracking"

    def test_json_output_emits_result(self, capsys):
        from jernerics.commands.tracking import replay

        result = ReplayResult(files_processed=1, events_sent=3, errors=["boom"])
        with patch("jernerics.commands.tracking.replay_tracking", return_value=result):
            replay(
                server="http://srv:8000",
                tracking_dir=Path("/tmp/trk"),
                json_output=True,
            )

        out = json.loads(capsys.readouterr().out)
        assert out["files_processed"] == 1
        assert out["events_sent"] == 3
        assert out["errors"] == ["boom"]

    def test_no_server_configured_exits(self):
        from jernerics.commands.tracking import replay
        from jernerics.config import ExitCode

        with (
            patch(
                "jernerics.commands.tracking.load_tracking_server", return_value=None
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            replay()

        assert exc_info.value.code == ExitCode.CONFIG_ERROR

    def test_dry_run_counts_local_events_json(self, tmp_path, capsys):
        from jernerics.commands.tracking import replay

        _write_jsonl(tmp_path / "sweep1" / "events" / "0.jsonl", 10)

        replay(
            server="http://srv:8000",
            tracking_dir=tmp_path,
            dry_run=True,
            json_output=True,
        )

        report = json.loads(capsys.readouterr().out)
        assert report == [{"study": "sweep1", "local": 10}]

    def test_dry_run_scoped_to_study(self, tmp_path, capsys):
        from jernerics.commands.tracking import replay

        _write_jsonl(tmp_path / "alpha" / "events" / "0.jsonl", 4)
        _write_jsonl(tmp_path / "beta" / "events" / "0.jsonl", 6)

        replay(
            server="http://srv:8000",
            tracking_dir=tmp_path,
            study="beta",
            dry_run=True,
            json_output=True,
        )

        report = json.loads(capsys.readouterr().out)
        assert report == [{"study": "beta", "local": 6}]

    def test_dry_run_text_output(self, tmp_path, capsys):
        from jernerics.commands.tracking import replay

        _write_jsonl(tmp_path / "sweep1" / "events" / "0.jsonl", 10)

        replay(
            server="http://srv:8000",
            tracking_dir=tmp_path,
            dry_run=True,
        )

        out = capsys.readouterr().out
        assert "sweep1: 10 local events" in out
        assert "Total: 10 local events" in out
        assert "dry run" in out

    def test_dry_run_no_local_events(self, tmp_path, capsys):
        from jernerics.commands.tracking import replay

        replay(
            server="http://srv:8000",
            tracking_dir=tmp_path,
            dry_run=True,
            json_output=True,
        )

        assert json.loads(capsys.readouterr().out) == []

    def test_backend_mode_syncs_from_backend(self):
        from jernerics.commands.tracking import replay

        backend = MagicMock()
        with (
            patch(
                "jernerics.commands.tracking._get_backend",
                return_value=(backend, "p", Path("/proj")),
            ) as get_backend,
            patch(
                "jernerics.commands.tracking.get_project_name",
                return_value="proj",
            ) as project_name,
        ):
            replay(backend_name="hpc", study="sweep1")

        get_backend.assert_called_once_with("hpc")
        project_name.assert_called_once_with(Path("/proj"))
        backend.sync.assert_called_once_with("proj", study="sweep1")

    @pytest.mark.parametrize(
        ("flag", "kwargs"),
        [
            ("--tracking-dir", {"tracking_dir": Path("/tmp/trk")}),
            ("--server", {"server": "http://srv:8000"}),
            ("--dry-run", {"dry_run": True}),
            ("--json", {"json_output": True}),
        ],
    )
    def test_backend_mode_rejects_local_only_flags(self, flag, kwargs, capsys):
        from jernerics.commands.tracking import replay
        from jernerics.config import ExitCode

        with (
            patch("jernerics.commands.tracking._get_backend") as get_backend,
            pytest.raises(SystemExit) as exc_info,
        ):
            replay(backend_name="hpc", **kwargs)

        assert exc_info.value.code == ExitCode.GENERAL_ERROR
        get_backend.assert_not_called()
        assert f"{flag} cannot be combined with --backend" in capsys.readouterr().out


class TestJobsCommand:
    def test_jobs_table_renders_study_column(self, capsys, tmp_path):
        from jernerics.backend.models import JobInfo
        from jernerics.commands.jobs import list_jobs

        backend = MagicMock()
        backend.list_jobs.return_value = [
            JobInfo(
                job_id="26887165",
                name="job",
                status="RUNNING",
                study_name="overfit_seed42",
            ),
            JobInfo(job_id="26887168", name="build", status="RUNNING"),
        ]
        with (
            patch(
                "jernerics.commands.jobs._get_backend",
                return_value=(backend, "p", tmp_path),
            ),
            patch("jernerics.commands.jobs.cache_dir", return_value=tmp_path),
        ):
            list_jobs(backend_name="hpc")

        out = capsys.readouterr().out
        assert "STUDY" in out
        assert "overfit_seed42" in out
        assert "—" in out

    def test_jobs_json_includes_study_name(self, capsys, tmp_path):
        from jernerics.backend.models import JobInfo
        from jernerics.commands.jobs import list_jobs

        backend = MagicMock()
        backend.list_jobs.return_value = [
            JobInfo(
                job_id="1",
                name="job",
                status="RUNNING",
                study_name="overfit_seed42",
            ),
        ]
        with (
            patch(
                "jernerics.commands.jobs._get_backend",
                return_value=(backend, "p", tmp_path),
            ),
            patch("jernerics.commands.jobs.cache_dir", return_value=tmp_path),
        ):
            list_jobs(backend_name="hpc", json_output=True)

        data = json.loads(capsys.readouterr().out)
        assert data[0]["study_name"] == "overfit_seed42"


class TestTraceCommand:
    def test_trace_json_scalar(self, capsys):
        from jernerics.commands.tracking import trace

        canned = {
            "value_type": "scalar",
            "series": [
                {"step": 0, "value": 9.0, "seq": 100, "timestamp_ns": 100},
                {"step": 1, "value": 0.5, "seq": 101, "timestamp_ns": 101},
            ],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_metric_series", return_value=canned),
        ):
            trace("s", "loss", json_output=True)

        out = json.loads(capsys.readouterr().out)
        assert out["metric"] == "loss"
        assert out["value_type"] == "scalar"
        assert len(out["series"]) == 2
        assert out["series"][0]["value"] == pytest.approx(9.0)

    def test_trace_json_text(self, capsys):
        from jernerics.commands.tracking import trace

        canned = {
            "value_type": "json",
            "series": [
                {"step": 0, "value": "<BOS>", "seq": 100, "timestamp_ns": 100},
                {"step": 1, "value": "mul add", "seq": 101, "timestamp_ns": 101},
            ],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_metric_series", return_value=canned),
        ):
            trace("s", "pred_expr", json_output=True)

        out = json.loads(capsys.readouterr().out)
        assert out["value_type"] == "json"
        assert out["series"][0]["value"] == "<BOS>"

    def test_trace_text_scalar_output(self, capsys):
        from jernerics.commands.tracking import trace

        canned = {
            "value_type": "scalar",
            "series": [
                {"step": 0, "value": 9.738, "seq": 2, "timestamp_ns": 100},
                {"step": 199, "value": 0.032, "seq": 5, "timestamp_ns": 200},
            ],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_metric_series", return_value=canned),
        ):
            trace("s", "loss")

        out = capsys.readouterr().out
        assert "Trace:" in out
        assert "loss" in out
        assert "9.738" in out or "9.738" in out
        assert "0.032" in out

    def test_trace_text_text_output(self, capsys):
        from jernerics.commands.tracking import trace

        canned = {
            "value_type": "json",
            "series": [
                {"step": 0, "value": "<BOS>", "seq": 2, "timestamp_ns": 100},
            ],
        }
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_metric_series", return_value=canned),
        ):
            trace("s", "pred_expr")

        out = capsys.readouterr().out
        assert "pred_expr" in out
        assert "<BOS>" in out

    def test_trace_missing_metric_exits(self, capsys):
        from jernerics.commands.tracking import trace

        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_metric_series", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            trace("s", "ghost", json_output=False)

        assert exc_info.value.code == 1
        assert "ghost" in capsys.readouterr().out

    def test_trace_missing_run_exits(self, capsys):
        from jernerics.commands.tracking import trace

        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            trace("ghost", "loss", json_output=False)

        assert exc_info.value.code == 1

    def test_trace_no_metric_lists_available(self, capsys):
        from jernerics.commands.tracking import trace

        canned_keys = [
            {"key": "loss", "value_type": "scalar", "count": 5},
            {"key": "pred_expr", "value_type": "json", "count": 3},
        ]
        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch(
                "jernerics.commands.tracking.get_metric_keys", return_value=canned_keys
            ),
        ):
            trace("s")

        out = capsys.readouterr().out
        assert "loss" in out
        assert "pred_expr" in out
        assert "scalar" in out
        assert "json" in out

    def test_trace_no_metric_no_metrics_message(self, capsys):
        from jernerics.commands.tracking import trace

        with (
            patch(
                "jernerics.commands.tracking._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.commands.tracking.run_exists", return_value=True),
            patch("jernerics.commands.tracking.get_metric_keys", return_value=[]),
        ):
            trace("s")

        out = capsys.readouterr().out
        assert "No metrics" in out


def _interactive_session_stub(existing):
    sess = MagicMock()
    sess.login_target = "jez@hpc"
    sess.remote_dir = "/home/jez/proj"
    sess.cache_host = "/home/jez/.cache/jernerics"
    sess.container_image = "/home/jez/proj/container.sif"
    sess.gpus = 1
    sess.partition = "gpu"
    sess.time_limit = "1:00:00"
    sess.find_existing.return_value = existing
    sess.host.run.return_value = MagicMock(returncode=0)
    return sess


def _sync_record(name="jernerics-interactive-proj", **overrides):
    record = SessionInfo(
        name=name,
        status="Watching",
        alpha_path="/local",
        beta_path="jez@hpc:/home/jez/proj",
        alpha_connected=True,
        beta_connected=True,
        conflicts=0,
    )
    for field, value in overrides.items():
        setattr(record, field, value)
    return record


class TestEnsureInteractiveSync:
    """``_ensure_interactive_sync``: start, resume, restart, and fallback."""

    def _session(self, login_target="jez@hpc", remote_dir="/home/jez/proj"):
        sess = MagicMock()
        sess.login_target = login_target
        sess.remote_dir = remote_dir
        sess.host = MagicMock()
        return sess

    def test_new_session_creates_sync(self):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session()
        with (
            patch("jernerics.commands.interactive.MutagenSync") as ms,
            patch("jernerics.commands.interactive.ProjectSync") as ps,
        ):
            ms.available.return_value = True
            ms.return_value.list_sessions.return_value = []
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=False)
            called = ms.return_value.start.call_args
            assert called.args[0] == Path("/proj")
            assert called.args[1] == "jez@hpc"
            assert called.args[2] == "/home/jez/proj"
            assert called.kwargs["name"] == "jernerics-interactive-proj"
            ps.assert_not_called()

    def test_new_session_replaces_stale_session(self, capsys):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session()
        stale = _sync_record()
        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            ms.return_value.list_sessions.return_value = [stale]
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=False)
            ms.return_value.terminate.assert_called_once_with(
                "jernerics-interactive-proj"
            )
            ms.return_value.start.assert_called_once()
        assert "Replacing stale" in capsys.readouterr().out

    def test_reconnect_keeps_live_session(self, capsys):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session()
        live = _sync_record()
        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            ms.return_value.list_sessions.return_value = [live]
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=True)
            ms.return_value.start.assert_not_called()
        assert "already running" in capsys.readouterr().out

    def test_reconnect_restarts_dead_session(self, capsys):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session()
        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            ms.return_value.list_sessions.return_value = []
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=True)
            ms.return_value.start.assert_called_once()
        assert "was lost" in capsys.readouterr().out

    def test_reconnect_conflicted_session_reports_without_restart(self, capsys):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session()
        with (
            patch("jernerics.commands.interactive.MutagenSync") as ms,
            patch("jernerics.commands.interactive.ProjectSync") as ps,
        ):
            ms.available.return_value = True
            ms.return_value.list_sessions.return_value = [_sync_record(conflicts=2)]
            ms.return_value.conflicted_paths.return_value = ["src/a.py", "src/b.py"]
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=True)
            ms.return_value.start.assert_not_called()
            ms.return_value.terminate.assert_not_called()
            ps.return_value.sync_project.assert_not_called()
        out = capsys.readouterr().out
        assert "not healthy" in out
        assert "conflicts: 2" in out
        assert "src/a.py" in out
        assert "src/b.py" in out
        assert "do not propagate in either direction" in out
        assert "mutagen sync list --long" in out
        assert "Resolve by making both sides agree" in out

    def test_fresh_start_with_conflicts_warns_without_error(self, capsys):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session()
        with (
            patch("jernerics.commands.interactive.MutagenSync") as ms,
            patch("jernerics.commands.interactive.ProjectSync") as ps,
        ):
            ms.available.return_value = True
            ms.return_value.list_sessions.side_effect = [
                [],
                [_sync_record(conflicts=1)],
            ]
            ms.return_value.conflicted_paths.return_value = ["src/c.py"]
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=False)
            ms.return_value.start.assert_called_once()
            ps.return_value.sync_project.assert_not_called()
        out = capsys.readouterr().out
        assert "Code sync is live" in out
        assert "conflicts: 1" in out
        assert "src/c.py" in out

    def test_fallback_when_mutagen_missing(self, capsys):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session()
        with (
            patch("jernerics.commands.interactive.MutagenSync") as ms,
            patch("jernerics.commands.interactive.ProjectSync") as ps,
        ):
            ms.available.return_value = False
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=False)
            ms.return_value.start.assert_not_called()
            ps.assert_called_once_with(sess.host, "/home/jez/proj")
            ps.return_value.sync_project.assert_called_once_with(Path("/proj"))
        assert "mutagen not found" in capsys.readouterr().out

    def test_start_failure_falls_back_to_oneshot(self):
        from jernerics.commands.interactive import _ensure_interactive_sync
        from jernerics.sync.mutagen_sync import MutagenError

        sess = self._session()
        with (
            patch("jernerics.commands.interactive.MutagenSync") as ms,
            patch("jernerics.commands.interactive.ProjectSync") as ps,
        ):
            ms.available.return_value = True
            ms.return_value.start.side_effect = MutagenError("boom")
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=False)
            ps.return_value.sync_project.assert_called_once()

    def test_no_host_is_noop(self):
        from jernerics.commands.interactive import _ensure_interactive_sync

        sess = self._session(login_target=None)
        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            _ensure_interactive_sync(sess, Path("/proj"), "proj", reconnect=False)
            ms.available.assert_not_called()


class TestTerminateInteractiveSync:
    def test_terminates_named_session(self, capsys):
        from jernerics.commands.interactive import _terminate_interactive_sync

        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            _terminate_interactive_sync("proj")
            ms.return_value.terminate.assert_called_once_with(
                "jernerics-interactive-proj"
            )
        assert "Stopped code sync" in capsys.readouterr().out

    def test_noop_when_mutagen_missing(self):
        from jernerics.commands.interactive import _terminate_interactive_sync

        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = False
            _terminate_interactive_sync("proj")
            ms.return_value.terminate.assert_not_called()

    def test_swallows_terminate_error(self):
        from jernerics.commands.interactive import _terminate_interactive_sync
        from jernerics.sync.mutagen_sync import MutagenError

        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            ms.return_value.terminate.side_effect = MutagenError("nope")
            _terminate_interactive_sync("proj")


class TestWarnSyncOrphans:
    def test_warns_about_orphans(self, capsys):
        from jernerics.commands.interactive import _warn_sync_orphans

        orphan = MagicMock()
        orphan.name = "jernerics-interactive-dead"
        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            ms.return_value.find_orphans.return_value = [orphan]
            _warn_sync_orphans("proj", alive=False)
        out = capsys.readouterr().out
        assert "stale sync session" in out
        assert "jernerics-interactive-dead" in out

    def test_silent_when_no_orphans(self, capsys):
        from jernerics.commands.interactive import _warn_sync_orphans

        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            ms.return_value.find_orphans.return_value = []
            _warn_sync_orphans("proj", alive=False)
        assert "stale" not in capsys.readouterr().out

    def test_alive_marks_current_as_live(self):
        from jernerics.commands.interactive import _warn_sync_orphans

        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = True
            ms.return_value.find_orphans.return_value = []
            _warn_sync_orphans("proj", alive=True)
            ms.return_value.find_orphans.assert_called_once_with(
                alive_names={"jernerics-interactive-proj"}
            )

    def test_noop_when_mutagen_missing(self):
        from jernerics.commands.interactive import _warn_sync_orphans

        with patch("jernerics.commands.interactive.MutagenSync") as ms:
            ms.available.return_value = False
            _warn_sync_orphans("proj", alive=False)
            ms.return_value.find_orphans.assert_not_called()


class TestInteractiveSyncWiring:
    """The ``interactive`` command threads sync through every lifecycle path."""

    def test_end_terminates_sync_before_scancel(self):
        from jernerics.commands.interactive import stop

        existing = MagicMock(job_id="123", state="RUNNING", node="gpu1")
        sess = _interactive_session_stub(existing)
        order: list[str] = []

        def record_terminate(*args, **kwargs):
            order.append("terminate")

        def record_end(*args, **kwargs):
            order.append("end")
            return existing

        with (
            patch(
                "jernerics.commands.interactive._build_interactive_session",
                return_value=(sess, Path("/proj"), "proj"),
            ),
            patch("jernerics.commands.interactive._terminate_interactive_sync") as term,
        ):
            term.side_effect = record_terminate
            sess.end.side_effect = record_end
            stop(backend_name="hpc")

        assert order == ["terminate", "end"]

    def test_new_session_starts_sync_then_connects(self):
        from jernerics.commands.interactive import start

        sess = _interactive_session_stub(None)
        sess.submit.return_value = "123"
        sess.wait_for_running.return_value = "gpu1"

        with (
            patch(
                "jernerics.commands.interactive._build_interactive_session",
                return_value=(sess, Path("/proj"), "proj"),
            ),
            patch("jernerics.commands.interactive._warn_sync_orphans") as warn,
            patch("jernerics.commands.interactive._ensure_interactive_sync") as ensure,
        ):
            start(backend_name="hpc")

        ensure.assert_called_once_with(sess, Path("/proj"), "proj", reconnect=False)
        sess.connect.assert_called_once_with("gpu1")
        warn.assert_called_once_with("proj", alive=False)

    def test_reconnect_running_uses_reconnect_sync(self):
        from jernerics.commands.interactive import start

        existing = MagicMock(job_id="123", state="RUNNING", node="gpu1")
        sess = _interactive_session_stub(existing)

        with (
            patch(
                "jernerics.commands.interactive._build_interactive_session",
                return_value=(sess, Path("/proj"), "proj"),
            ),
            patch("jernerics.commands.interactive._warn_sync_orphans") as warn,
            patch("jernerics.commands.interactive._ensure_interactive_sync") as ensure,
        ):
            start(backend_name="hpc")

        ensure.assert_called_once_with(sess, Path("/proj"), "proj", reconnect=True)
        sess.connect.assert_called_once_with("gpu1")
        warn.assert_called_once_with("proj", alive=True)


class TestSyncStatusCommand:
    """``interactive sync status``: read-only report of the mutagen session."""

    def _run(self, record, *, json_output=False, paths=None):
        from jernerics.commands.interactive import sync_status

        with (
            patch(
                "jernerics.commands.interactive.find_pyproject_dir",
                return_value=Path("/proj"),
            ),
            patch("jernerics.commands.interactive.load_backend_config"),
            patch(
                "jernerics.commands.interactive.get_project_name",
                return_value="proj",
            ),
            patch("jernerics.commands.interactive.MutagenSync") as ms,
        ):
            ms.available.return_value = True
            ms.return_value.list_sessions.return_value = (
                [record] if record is not None else []
            )
            ms.return_value.conflicted_paths.return_value = paths or []
            sync_status(backend_name="hpc", json_output=json_output)

    def test_healthy_session_human_output(self, capsys):
        self._run(_sync_record())
        out = capsys.readouterr().out
        assert "Session:   jernerics-interactive-proj" in out
        assert "Status:    Watching" in out
        assert "Local:     connected" in out
        assert "Cluster:   connected" in out
        assert "Idle:      yes" in out
        assert "Converged: yes" in out
        assert "Conflicts: 0" in out

    def test_healthy_session_json_output(self, capsys):
        self._run(_sync_record(), json_output=True)
        report = json.loads(capsys.readouterr().out)
        assert report == {
            "project": "proj",
            "backend": "hpc",
            "session": "jernerics-interactive-proj",
            "exists": True,
            "status": "Watching",
            "local_connected": True,
            "cluster_connected": True,
            "idle": True,
            "converged": True,
            "conflicts": 0,
            "conflicted_paths": [],
        }

    def test_conflicted_session_lists_paths(self, capsys):
        self._run(_sync_record(conflicts=2), paths=["src/a.py", "src/b.py"])
        out = capsys.readouterr().out
        assert "Conflicts: 2" in out
        assert "src/a.py" in out
        assert "src/b.py" in out
        assert "do not propagate" in out

    def test_conflicted_session_json_paths(self, capsys):
        self._run(
            _sync_record(conflicts=2),
            json_output=True,
            paths=["src/a.py", "src/b.py"],
        )
        report = json.loads(capsys.readouterr().out)
        assert report["conflicts"] == 2
        assert report["conflicted_paths"] == ["src/a.py", "src/b.py"]
        assert report["converged"] is False

    def test_disconnected_endpoints_reported_without_error(self, capsys):
        self._run(
            _sync_record(
                status="Connecting", alpha_connected=False, beta_connected=False
            ),
            json_output=True,
        )
        report = json.loads(capsys.readouterr().out)
        assert report["local_connected"] is False
        assert report["cluster_connected"] is False
        assert report["idle"] is False

    def test_syncing_session_reported_without_error(self, capsys):
        self._run(_sync_record(status="Scanning"), json_output=True)
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "Scanning"
        assert report["idle"] is False
        assert report["converged"] is False

    def test_missing_session_json_shape(self, capsys):
        self._run(None, json_output=True)
        report = json.loads(capsys.readouterr().out)
        assert report == {
            "project": "proj",
            "backend": "hpc",
            "session": "jernerics-interactive-proj",
            "exists": False,
            "status": None,
            "local_connected": None,
            "cluster_connected": None,
            "idle": False,
            "converged": False,
            "conflicts": 0,
            "conflicted_paths": [],
        }

    def test_missing_session_human_output(self, capsys):
        self._run(None)
        out = capsys.readouterr().out
        assert "jernerics-interactive-proj not found" in out

    def test_unknown_backend_exits_config_error(self, capsys):
        from jernerics.commands.interactive import sync_status
        from jernerics.config import ConfigNotFound, ExitCode

        with (
            patch(
                "jernerics.commands.interactive.find_pyproject_dir",
                return_value=Path("/proj"),
            ),
            patch(
                "jernerics.commands.interactive.load_backend_config",
                side_effect=ConfigNotFound("unknown backend 'nope'"),
            ),
            pytest.raises(SystemExit) as exc,
        ):
            sync_status(backend_name="nope")
        assert exc.value.code == ExitCode.CONFIG_ERROR
        assert "unknown backend" in capsys.readouterr().out

    def test_no_pyproject_exits_config_error(self):
        from jernerics.commands.interactive import sync_status
        from jernerics.config import ExitCode

        with (
            patch(
                "jernerics.commands.interactive.find_pyproject_dir", return_value=None
            ),
            pytest.raises(SystemExit) as exc,
        ):
            sync_status(backend_name="hpc")
        assert exc.value.code == ExitCode.CONFIG_ERROR

    def test_mutagen_missing_exits_error(self, capsys):
        from jernerics.commands.interactive import sync_status
        from jernerics.config import ExitCode

        with (
            patch(
                "jernerics.commands.interactive.find_pyproject_dir",
                return_value=Path("/proj"),
            ),
            patch("jernerics.commands.interactive.load_backend_config"),
            patch(
                "jernerics.commands.interactive.get_project_name",
                return_value="proj",
            ),
            patch("jernerics.commands.interactive.MutagenSync") as ms,
        ):
            ms.available.return_value = False
            with pytest.raises(SystemExit) as exc:
                sync_status(backend_name="hpc")
        assert exc.value.code == ExitCode.GENERAL_ERROR
        assert "mutagen not found" in capsys.readouterr().out

    def test_mutagen_failure_exits_error(self, capsys):
        from jernerics.commands.interactive import sync_status
        from jernerics.config import ExitCode
        from jernerics.sync.mutagen_sync import MutagenError

        with (
            patch(
                "jernerics.commands.interactive.find_pyproject_dir",
                return_value=Path("/proj"),
            ),
            patch("jernerics.commands.interactive.load_backend_config"),
            patch(
                "jernerics.commands.interactive.get_project_name",
                return_value="proj",
            ),
            patch("jernerics.commands.interactive.MutagenSync") as ms,
        ):
            ms.available.return_value = True
            ms.return_value.list_sessions.side_effect = MutagenError("boom")
            with pytest.raises(SystemExit) as exc:
                sync_status(backend_name="hpc")
        assert exc.value.code == ExitCode.GENERAL_ERROR

    def test_status_uses_only_read_only_mutagen_calls(self, capsys):
        from subprocess import CompletedProcess

        from jernerics.commands.interactive import sync_status

        listing = (
            "jernerics-interactive-proj\tWatching\t/local\tjez@hpc:/remote"
            "\ttrue\ttrue\t2\n"
        )
        conflicts = "jernerics-interactive-proj\tsrc/a.py\tsrc/b.py\n"
        outputs = [listing, conflicts]
        calls: list[list[str]] = []

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return CompletedProcess(
                args=argv,
                returncode=0,
                stdout=outputs[min(len(calls) - 1, len(outputs) - 1)],
                stderr="",
            )

        with (
            patch("shutil.which", return_value="/usr/bin/mutagen"),
            patch(
                "jernerics.commands.interactive.find_pyproject_dir",
                return_value=Path("/proj"),
            ),
            patch("jernerics.commands.interactive.load_backend_config"),
            patch(
                "jernerics.commands.interactive.get_project_name",
                return_value="proj",
            ),
            patch("jernerics.sync.mutagen_sync.subprocess.run", side_effect=fake_run),
        ):
            sync_status(backend_name="hpc")

        assert len(calls) == 2
        for argv in calls:
            assert argv[1:3] == ["sync", "list"]
            assert not {
                "create",
                "terminate",
                "flush",
                "pause",
                "resume",
                "reset",
            } & set(argv)
        out = capsys.readouterr().out
        assert "Conflicts: 2" in out
        assert "src/a.py" in out
