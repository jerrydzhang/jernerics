import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jernerics.cli import (
    _create_minimal_pyproject,
    _get_default_jernerics_config,
)
from jernerics.tracking.batch_sync import ReplayResult
from jernerics.tracking.jsonl_io import TrackingWriter


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
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        assert (project_dir / "pyproject.toml").exists()

    def test_init_creates_container_def(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        assert (project_dir / "container.def").exists()

    def test_init_scaffolds_trial_and_config(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        trial = (project_dir / "trial.py").read_text()
        assert "def trial(config, tracker)" in trial
        assert (project_dir / "config.py").exists()

    def test_init_preserves_existing_trial_and_config(self, tmp_path):
        project_dir = tmp_path / "existing-project"
        project_dir.mkdir()
        (project_dir / "trial.py").write_text("# my trial")
        (project_dir / "config.py").write_text("# my config")

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        assert (project_dir / "trial.py").read_text() == "# my trial"
        assert (project_dir / "config.py").read_text() == "# my config"

    def test_init_requires_uv(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None

            from jernerics.cli import init

            with pytest.raises(SystemExit):
                init(str(project_dir))

    def test_init_invalid_starter(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"

            from jernerics.cli import init

            with pytest.raises(SystemExit):
                init(str(project_dir), starter="nonexistent")

    def test_init_preserves_existing_container_def(self, tmp_path):
        project_dir = tmp_path / "existing-project"
        project_dir.mkdir()
        (project_dir / "container.def").write_text("existing definition")

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        assert (project_dir / "container.def").read_text() == "existing definition"


class TestMainFunction:
    def test_main_calls_app(self):
        from jernerics.cli import main

        with patch("jernerics.cli.app") as mock_app:
            main()
            mock_app.assert_called_once()


class TestWaitCommand:
    def test_wait_calls_backend_with_correct_args(self, capsys):
        from jernerics.cli import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.return_value = True

        with patch(
            "jernerics.cli._get_backend",
            return_value=(mock_backend, "proj", Path("/tmp")),
        ):
            wait("job-123", backend_name="hpc", timeout=None, poll_interval=10)

        mock_backend.wait_for_completion.assert_called_once_with(
            "job-123", poll_interval=10, timeout=None
        )
        out = capsys.readouterr().out
        assert "completed successfully" in out

    def test_wait_exit_zero_on_success(self, capsys):
        from jernerics.cli import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.return_value = True

        with patch(
            "jernerics.cli._get_backend",
            return_value=(mock_backend, "proj", Path("/tmp")),
        ):
            wait("job-123", backend_name="hpc", timeout=None, poll_interval=10)

        out = capsys.readouterr().out
        assert "Job job-123 completed successfully" in out

    def test_wait_exit_one_on_failure(self, capsys):
        from jernerics.cli import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.return_value = False

        with (
            patch(
                "jernerics.cli._get_backend",
                return_value=(mock_backend, "proj", Path("/tmp")),
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            wait("job-123", backend_name="hpc", timeout=None, poll_interval=10)

        assert exc_info.value.code == 1
        out = capsys.readouterr().out
        assert "finished with non-success status" in out

    def test_wait_exit_two_on_timeout(self, capsys):
        from jernerics.cli import wait

        mock_backend = MagicMock()
        mock_backend.wait_for_completion.side_effect = TimeoutError("timed out")

        with (
            patch(
                "jernerics.cli._get_backend",
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
        from jernerics.cli import runs

        canned = [{"study_name": "s", "trial_id": 0, "status": "running"}]
        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.get_all_runs", return_value=canned),
        ):
            runs(json_output=True)

        assert json.loads(capsys.readouterr().out) == canned

    def test_runs_text_renders_without_error(self, capsys):
        from jernerics.cli import runs

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
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.get_all_runs", return_value=canned),
        ):
            runs(json_output=False)

        out = capsys.readouterr().out
        assert "s" in out
        assert "completed" in out

    def test_runs_empty_message(self, capsys):
        from jernerics.cli import runs

        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.get_all_runs", return_value=[]),
        ):
            runs(json_output=False)

        assert "No runs found" in capsys.readouterr().out


class TestSummaryCommand:
    def test_summary_json_outputs_analysis(self, capsys):
        from jernerics.cli import summary

        canned = {
            "study_name": "s",
            "metrics": {},
            "params": {},
            "artifacts": [],
        }
        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=True)

        assert json.loads(capsys.readouterr().out) == canned

    def test_summary_missing_run_exits(self, capsys):
        from jernerics.cli import summary

        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            summary("ghost", json_output=False)

        assert exc_info.value.code == 1

    def test_summary_text_renders_git_hash(self, capsys):
        from jernerics.cli import summary

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
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=False)

        out = capsys.readouterr().out
        assert "git: 4a5e109" in out

    def test_summary_text_renders_text_metrics_section(self, capsys):
        from jernerics.cli import summary

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
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=False)

        out = capsys.readouterr().out
        assert "Text metrics:" in out
        assert "pred_expr (5 points)" in out

    def test_summary_text_omits_text_metrics_when_absent(self, capsys):
        from jernerics.cli import summary

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
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_run_summary", return_value=canned),
        ):
            summary("s", json_output=False)

        out = capsys.readouterr().out
        assert "Text metrics:" not in out


class TestDiffCommand:
    def test_diff_json_outputs_analysis(self, capsys):
        from jernerics.cli import diff

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
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_run_diff", return_value=canned),
        ):
            diff("a", "b", json_output=True)

        assert json.loads(capsys.readouterr().out) == canned

    def test_diff_missing_run_exits(self, capsys):
        from jernerics.cli import diff

        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            diff("a", "b", json_output=False)

        assert exc_info.value.code == 1


def _value_envelope(seq: int, study: str = "sweep1") -> dict:
    return {
        "project": "p",
        "study_name": study,
        "trial_id": 0,
        "run_id": 0,
        "timestamp_ns": 1000 + seq,
        "seq": seq,
        "value": {"key": "loss", "value": 0.5, "step": 0, "context": "{}"},
    }


def _write_jsonl(path: Path, n: int, study: str = "sweep1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with TrackingWriter(path) as writer:
        for i in range(n):
            writer.write_envelope(_value_envelope(i, study))


class TestReplayCommand:
    def test_passes_resolved_server_and_dir_to_replay(self):
        from jernerics.cli import replay

        with patch(
            "jernerics.cli.replay_tracking", return_value=ReplayResult()
        ) as mock_rt:
            replay(server="http://srv:8000", tracking_dir=Path("/tmp/trk"))

        kwargs = mock_rt.call_args.kwargs
        assert kwargs["base_url"] == "http://srv:8000"
        assert kwargs["tracking_dir"] == Path("/tmp/trk")
        assert kwargs["study"] is None

    def test_resolves_tracking_dir_from_cache_when_unset(self, tmp_path):
        from jernerics.cli import replay

        with (
            patch(
                "jernerics.cli.replay_tracking", return_value=ReplayResult()
            ) as mock_rt,
            patch("jernerics.cli.cache_dir", return_value=tmp_path),
        ):
            replay(server="http://srv:8000")

        assert mock_rt.call_args.kwargs["tracking_dir"] == tmp_path / "tracking"

    def test_json_output_emits_result(self, capsys):
        from jernerics.cli import replay

        result = ReplayResult(files_processed=1, events_sent=3, errors=["boom"])
        with patch("jernerics.cli.replay_tracking", return_value=result):
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
        from jernerics.cli import replay
        from jernerics.config import ExitCode

        with (
            patch("jernerics.cli.load_tracking_server", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            replay()

        assert exc_info.value.code == ExitCode.CONFIG_ERROR

    def test_dry_run_computes_delta_json(self, tmp_path, capsys):
        from jernerics.cli import replay

        _write_jsonl(tmp_path / "sweep1" / "events" / "0.jsonl", 10)
        store = MagicMock()
        store.query.return_value = (["COUNT(*)"], [(1,)])

        with patch("jernerics.cli.RemoteStore", return_value=store):
            replay(
                server="http://srv:8000",
                tracking_dir=tmp_path,
                dry_run=True,
                json_output=True,
            )

        report = json.loads(capsys.readouterr().out)
        assert report == [{"study": "sweep1", "local": 10, "synced": 5, "new": 5}]

    def test_dry_run_scoped_to_study(self, tmp_path, capsys):
        from jernerics.cli import replay

        _write_jsonl(tmp_path / "alpha" / "events" / "0.jsonl", 4)
        _write_jsonl(tmp_path / "beta" / "events" / "0.jsonl", 6)
        store = MagicMock()
        store.query.return_value = (["COUNT(*)"], [(0,)])

        with patch("jernerics.cli.RemoteStore", return_value=store):
            replay(
                server="http://srv:8000",
                tracking_dir=tmp_path,
                study="beta",
                dry_run=True,
                json_output=True,
            )

        report = json.loads(capsys.readouterr().out)
        assert report == [{"study": "beta", "local": 6, "synced": 0, "new": 6}]

    def test_dry_run_text_output(self, tmp_path, capsys):
        from jernerics.cli import replay

        _write_jsonl(tmp_path / "sweep1" / "events" / "0.jsonl", 10)
        store = MagicMock()
        store.query.return_value = (["COUNT(*)"], [(3,)])

        with patch("jernerics.cli.RemoteStore", return_value=store):
            replay(
                server="http://srv:8000",
                tracking_dir=tmp_path,
                dry_run=True,
            )

        out = capsys.readouterr().out
        assert "sweep1: 10 local, 15 synced, 0 would be new" in out
        assert "dry run" in out

    def test_dry_run_no_local_events(self, tmp_path, capsys):
        from jernerics.cli import replay

        store = MagicMock()
        with patch("jernerics.cli.RemoteStore", return_value=store):
            replay(
                server="http://srv:8000",
                tracking_dir=tmp_path,
                dry_run=True,
                json_output=True,
            )

        assert json.loads(capsys.readouterr().out) == []
        store.query.assert_not_called()


class TestJobsCommand:
    def test_jobs_table_renders_study_column(self, capsys, tmp_path):
        from jernerics.backend.models import JobInfo
        from jernerics.cli import jobs

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
            patch("jernerics.cli._get_backend", return_value=(backend, "p", tmp_path)),
            patch("jernerics.cli.cache_dir", return_value=tmp_path),
        ):
            jobs(backend_name="hpc")

        out = capsys.readouterr().out
        assert "STUDY" in out
        assert "overfit_seed42" in out
        assert "—" in out

    def test_jobs_json_includes_study_name(self, capsys, tmp_path):
        from jernerics.backend.models import JobInfo
        from jernerics.cli import jobs

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
            patch("jernerics.cli._get_backend", return_value=(backend, "p", tmp_path)),
            patch("jernerics.cli.cache_dir", return_value=tmp_path),
        ):
            jobs(backend_name="hpc", json_output=True)

        data = json.loads(capsys.readouterr().out)
        assert data[0]["study_name"] == "overfit_seed42"


class TestTraceCommand:
    def test_trace_json_scalar(self, capsys):
        from jernerics.cli import trace

        canned = {
            "value_type": "scalar",
            "series": [
                {"step": 0, "value": 9.0, "seq": 100, "timestamp_ns": 100},
                {"step": 1, "value": 0.5, "seq": 101, "timestamp_ns": 101},
            ],
        }
        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_metric_series", return_value=canned),
        ):
            trace("s", "loss", json_output=True)

        out = json.loads(capsys.readouterr().out)
        assert out["metric"] == "loss"
        assert out["value_type"] == "scalar"
        assert len(out["series"]) == 2
        assert out["series"][0]["value"] == 9.0

    def test_trace_json_text(self, capsys):
        from jernerics.cli import trace

        canned = {
            "value_type": "json",
            "series": [
                {"step": 0, "value": "<BOS>", "seq": 100, "timestamp_ns": 100},
                {"step": 1, "value": "mul add", "seq": 101, "timestamp_ns": 101},
            ],
        }
        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_metric_series", return_value=canned),
        ):
            trace("s", "pred_expr", json_output=True)

        out = json.loads(capsys.readouterr().out)
        assert out["value_type"] == "json"
        assert out["series"][0]["value"] == "<BOS>"

    def test_trace_text_scalar_output(self, capsys):
        from jernerics.cli import trace

        canned = {
            "value_type": "scalar",
            "series": [
                {"step": 0, "value": 9.738, "seq": 2, "timestamp_ns": 100},
                {"step": 199, "value": 0.032, "seq": 5, "timestamp_ns": 200},
            ],
        }
        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_metric_series", return_value=canned),
        ):
            trace("s", "loss")

        out = capsys.readouterr().out
        assert "Trace:" in out
        assert "loss" in out
        assert "9.738" in out or "9.738" in out
        assert "0.032" in out

    def test_trace_text_text_output(self, capsys):
        from jernerics.cli import trace

        canned = {
            "value_type": "json",
            "series": [
                {"step": 0, "value": "<BOS>", "seq": 2, "timestamp_ns": 100},
            ],
        }
        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_metric_series", return_value=canned),
        ):
            trace("s", "pred_expr")

        out = capsys.readouterr().out
        assert "pred_expr" in out
        assert "<BOS>" in out

    def test_trace_missing_metric_exits(self, capsys):
        from jernerics.cli import trace

        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_metric_series", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            trace("s", "ghost", json_output=False)

        assert exc_info.value.code == 1
        assert "ghost" in capsys.readouterr().out

    def test_trace_missing_run_exits(self, capsys):
        from jernerics.cli import trace

        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=False),
            pytest.raises(SystemExit) as exc_info,
        ):
            trace("ghost", "loss", json_output=False)

        assert exc_info.value.code == 1

    def test_trace_no_metric_lists_available(self, capsys):
        from jernerics.cli import trace

        canned_keys = [
            {"key": "loss", "value_type": "scalar", "count": 5},
            {"key": "pred_expr", "value_type": "json", "count": 3},
        ]
        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_metric_keys", return_value=canned_keys),
        ):
            trace("s")

        out = capsys.readouterr().out
        assert "loss" in out
        assert "pred_expr" in out
        assert "scalar" in out
        assert "json" in out

    def test_trace_no_metric_no_metrics_message(self, capsys):
        from jernerics.cli import trace

        with (
            patch(
                "jernerics.cli._get_tracking_store",
                return_value=(MagicMock(), "p"),
            ),
            patch("jernerics.cli.run_exists", return_value=True),
            patch("jernerics.cli.get_metric_keys", return_value=[]),
        ):
            trace("s")

        out = capsys.readouterr().out
        assert "No metrics" in out
