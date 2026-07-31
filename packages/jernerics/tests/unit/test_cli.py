import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from jernerics.cli import (
    _create_minimal_pyproject,
    _get_default_jernerics_config,
)


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
