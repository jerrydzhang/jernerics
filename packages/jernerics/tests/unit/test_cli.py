import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import ClassVar
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


class MockSweepsHandler(BaseHTTPRequestHandler):
    """Mock HTTP server handler for sweeps testing."""

    response_data: ClassVar[list[dict]] = []

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        if (
            self.path == "/api/sweeps"
            or self.path.startswith("/api/sweeps?")
            or self.path.startswith("/api/trials")
        ):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response_body = json.dumps(MockSweepsHandler.response_data).encode("utf-8")
            self.wfile.write(response_body)
        else:
            self.send_error(404, "Not Found")


@pytest.fixture
def mock_sweeps_server():
    """Start a mock HTTP server for sweeps testing."""
    server = HTTPServer(("localhost", 0), MockSweepsHandler)
    port = server.server_address[1]

    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://localhost:{port}"

    server.shutdown()


class TestSweepsCommand:
    def test_sweeps_with_server_option(self, mock_sweeps_server, capsys):
        MockSweepsHandler.response_data = [
            {
                "project": "my-project",
                "study_name": "study-1",
                "trial_count": 10,
                "completed_count": 5,
                "last_event_timestamp_ns": "2024-01-15T10:30:00Z",
            }
        ]

        from jernerics.cli import sweeps

        sweeps(server=mock_sweeps_server)

        captured = capsys.readouterr()
        assert "my-project" in captured.out
        assert "study-1" in captured.out
        assert "10" in captured.out
        assert "5" in captured.out

    def test_sweeps_with_env_var(self, mock_sweeps_server, capsys, monkeypatch):
        MockSweepsHandler.response_data = []

        from jernerics.cli import sweeps

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", mock_sweeps_server)
        sweeps(server=None)

        captured = capsys.readouterr()
        assert "No sweeps found" in captured.out

    def test_sweeps_with_json_flag(self, mock_sweeps_server, capsys):
        MockSweepsHandler.response_data = [
            {
                "project": "my-project",
                "study_name": "study-1",
                "trial_count": 10,
                "completed_count": 5,
                "last_event_timestamp_ns": "2024-01-15T10:30:00Z",
            }
        ]

        from jernerics.cli import sweeps

        sweeps(server=mock_sweeps_server, json_output=True)

        captured = capsys.readouterr()
        assert "my-project" in captured.out
        assert "study-1" in captured.out

    def test_sweeps_with_empty_response(self, mock_sweeps_server, capsys):
        MockSweepsHandler.response_data = []

        from jernerics.cli import sweeps

        sweeps(server=mock_sweeps_server)

        captured = capsys.readouterr()
        assert "No sweeps found" in captured.out

    def test_sweeps_server_url_priority(self, mock_sweeps_server, capsys, monkeypatch):
        MockSweepsHandler.response_data = []

        from jernerics.cli import sweeps

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", "http://wrong-url:9999")
        sweeps(server=mock_sweeps_server)

        captured = capsys.readouterr()
        assert "No sweeps found" in captured.out

    def test_sweeps_no_server_url_error(self, capsys, monkeypatch, tmp_path):
        from jernerics.cli import sweeps

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        (tmp_path / "pyproject.toml").write_text("[tool]\n[jernerics]\n")

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            sweeps(server=None)

        assert exc_info.value.code == 3

    def test_sweeps_displays_rich_table(self, mock_sweeps_server, capsys):
        MockSweepsHandler.response_data = [
            {
                "project": "project-a",
                "study_name": "study-1",
                "trial_count": 5,
                "completed_count": 2,
                "last_event_timestamp_ns": "2024-01-15T10:30:00Z",
            },
            {
                "project": "project-b",
                "study_name": "study-2",
                "trial_count": 20,
                "completed_count": 20,
                "last_event_timestamp_ns": "2024-01-15T11:00:00Z",
            },
        ]

        from jernerics.cli import sweeps

        sweeps(server=mock_sweeps_server)

        captured = capsys.readouterr()
        assert "project-a" in captured.out
        assert "project-b" in captured.out
        assert "study-1" in captured.out
        assert "study-2" in captured.out
        assert "PROJECT" in captured.out
        assert "STUDY_NAME" in captured.out
        assert "TRIALS" in captured.out
        assert "COMPLETED" in captured.out
        assert "LAST_EVENT" in captured.out

    def test_sweeps_with_project_flag(self, mock_sweeps_server, capsys):
        MockSweepsHandler.response_data = [
            {
                "project": "my-project",
                "study_name": "study-1",
                "trial_count": 10,
                "completed_count": 5,
                "last_event_timestamp_ns": "2024-01-15T10:30:00Z",
            }
        ]

        from jernerics.cli import sweeps

        sweeps(server=mock_sweeps_server, project="my-project")

        captured = capsys.readouterr()
        assert "my-project" in captured.out
        assert "study-1" in captured.out

    def test_sweeps_with_project_flag_url_encodes(self, mock_sweeps_server, capsys):
        MockSweepsHandler.response_data = [
            {
                "project": "my project",
                "study_name": "study-1",
                "trial_count": 10,
                "completed_count": 5,
                "last_event_timestamp_ns": "2024-01-15T10:30:00Z",
            }
        ]

        from jernerics.cli import sweeps

        sweeps(server=mock_sweeps_server, project="my project")

        captured = capsys.readouterr()
        assert "my project" in captured.out
        assert "study-1" in captured.out

    def test_sweeps_uses_correct_field_names(self, mock_sweeps_server, capsys):
        """Test that the CLI uses the correct field names from the server response.

        This test would fail if the CLI used the old field names
        (trials, completed, last_event) instead of the correct ones
        (trial_count, completed_count, last_event_timestamp_ns).
        """
        MockSweepsHandler.response_data = [
            {
                "project": "my-project",
                "study_name": "study-1",
                "trial_count": 15,
                "completed_count": 7,
                "last_event_timestamp_ns": "2024-01-15T10:30:00Z",
            }
        ]

        from jernerics.cli import sweeps

        sweeps(server=mock_sweeps_server)

        captured = capsys.readouterr()
        # Verify correct values (would be 0/0/blank if wrong field names)
        assert "15" in captured.out
        assert "7" in captured.out
        assert "2024-01-15T10:30:00Z" in captured.out


class MockTrialsHandler(BaseHTTPRequestHandler):
    """Mock HTTP server handler for trials testing."""

    response_data: ClassVar[list[dict] | dict] = []

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        if (
            self.path.startswith("/api/trials")
            or self.path.startswith("/api/compare-sweeps")
            or self.path.startswith("/api/artifacts")
            or self.path.startswith("/api/results")
        ):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response_data = MockTrialsHandler.response_data

            if self.path.startswith("/api/trials"):
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(self.path)
                query_params = parse_qs(parsed.query)
                if "metric_keys" in query_params:
                    metric_keys = [
                        k.strip() for k in query_params["metric_keys"][0].split(",")
                    ]
                    if isinstance(response_data, list):
                        for trial in response_data:
                            if "final_metrics" in trial:
                                filtered_metrics = {
                                    k: v
                                    for k, v in trial["final_metrics"].items()
                                    if k in metric_keys
                                }
                                trial["final_metrics"] = filtered_metrics

            response_body = json.dumps(response_data).encode("utf-8")
            self.wfile.write(response_body)
        else:
            self.send_error(404, "Not Found")


@pytest.fixture
def mock_trials_server():
    """Start a mock HTTP server for trials testing."""
    server = HTTPServer(("localhost", 0), MockTrialsHandler)
    port = server.server_address[1]

    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://localhost:{port}"

    server.shutdown()


class TestTrialsCommand:
    def test_trials_with_server_option(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01, "batch_size": 32},
                "final_metrics": {"accuracy": 0.95, "loss": 0.05},
                "artifact_keys": ["model.pkl", "log.txt"],
            }
        ]

        from jernerics.cli import trials

        trials(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "complete" in captured.out
        assert "0.95" in captured.out
        assert "0.05" in captured.out
        assert "2" in captured.out  # artifact_count

    def test_trials_with_env_var(self, mock_trials_server, capsys, monkeypatch):
        MockTrialsHandler.response_data = []

        from jernerics.cli import trials

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", mock_trials_server)
        trials(project="my-project", sweep="study-1", server=None)

        captured = capsys.readouterr()
        assert "No trials found" in captured.out

    def test_trials_with_json_flag(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            }
        ]

        from jernerics.cli import trials

        trials(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            json_output=True,
        )

        captured = capsys.readouterr()
        assert "trial_id" in captured.out
        assert "complete" in captured.out
        assert "0.01" in captured.out
        assert "0.95" in captured.out

    def test_trials_with_empty_response(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = []

        from jernerics.cli import trials

        trials(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "No trials found" in captured.out

    def test_trials_with_params_flag(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01, "batch_size": 32},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            }
        ]

        from jernerics.cli import trials

        trials(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            params=True,
        )

        captured = capsys.readouterr()
        assert "LR" in captured.out
        assert "BATCH_SIZE" in captured.out
        assert "0.01" in captured.out
        assert "32" in captured.out

    def test_trials_with_columns_option(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            }
        ]

        from jernerics.cli import trials

        trials(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            columns="trial_id,accuracy",
        )

        captured = capsys.readouterr()
        assert "TRIAL_ID" in captured.out
        assert "ACCURACY" in captured.out
        assert "1" in captured.out
        assert "0.95" in captured.out
        assert "STATUS" not in captured.out  # Should not show status column

    def test_trials_with_limit(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": i,
                "status": "complete",
                "params": {},
                "final_metrics": {},
                "artifact_keys": [],
            }
            for i in range(1, 11)
        ]

        from jernerics.cli import trials

        trials(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            limit=5,
        )

        captured = capsys.readouterr()
        # Should show only 5 trials
        assert "10" not in captured.out

    def test_trials_displays_rich_table(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            },
            {
                "trial_id": 2,
                "status": "incomplete",
                "params": {},
                "final_metrics": {},
                "artifact_keys": ["log.txt"],
            },
        ]

        from jernerics.cli import trials

        trials(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "2" in captured.out
        assert "complete" in captured.out
        assert "incomplete" in captured.out
        assert "TRIAL_ID" in captured.out
        assert "STATUS" in captured.out
        assert "ACCURACY" in captured.out
        assert "ARTIFACT_COUNT" in captured.out

    def test_trials_server_url_priority(self, mock_trials_server, capsys, monkeypatch):
        MockTrialsHandler.response_data = []

        from jernerics.cli import trials

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", "http://wrong-url:9999")
        trials(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "No trials found" in captured.out

    def test_trials_no_server_url_error(self, capsys, monkeypatch, tmp_path):
        from jernerics.cli import trials

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        (tmp_path / "pyproject.toml").write_text("[tool]\n[jernerics]\n")

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            trials(project="my-project", sweep="study-1", server=None)

        assert exc_info.value.code == 3


class TestCompareSweepsCommand:
    def test_compare_sweeps_with_server_option(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 10,
            "left_completed_count": 5,
            "right_trial_count": 20,
            "right_completed_count": 15,
            "param_keys": {
                "shared": ["lr", "batch_size"],
                "left_only": ["optimizer"],
                "right_only": ["momentum"],
            },
            "final_metric_keys": {
                "shared": ["accuracy"],
                "left_only": ["loss"],
                "right_only": [],
            },
            "artifact_keys": {
                "shared": ["model.pkl"],
                "left_only": [],
                "right_only": ["log.txt"],
            },
            "final_metric_stats": {
                "accuracy": {
                    "left": {"min": 0.8, "median": 0.9, "max": 0.95},
                    "right": {"min": 0.85, "median": 0.88, "max": 0.92},
                }
            },
        }

        from jernerics.cli import compare_sweeps as compare_sweeps_cmd

        compare_sweeps_cmd(
            project="my-project",
            left="study-1",
            right="study-2",
            server=mock_trials_server,
        )

        captured = capsys.readouterr()
        assert "study-1" in captured.out
        assert "study-2" in captured.out
        assert "10" in captured.out
        assert "20" in captured.out
        assert "accuracy" in captured.out
        assert "0.8" in captured.out

    def test_compare_sweeps_with_json_flag(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 10,
            "left_completed_count": 5,
            "right_trial_count": 20,
            "right_completed_count": 15,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        from jernerics.cli import compare_sweeps as compare_sweeps_cmd

        compare_sweeps_cmd(
            project="my-project",
            left="study-1",
            right="study-2",
            server=mock_trials_server,
            json_output=True,
        )

        captured = capsys.readouterr()
        assert "study-1" in captured.out
        assert "study-2" in captured.out
        assert "left_trial_count" in captured.out

    def test_compare_sweeps_displays_rich_table(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 10,
            "left_completed_count": 5,
            "right_trial_count": 20,
            "right_completed_count": 15,
            "param_keys": {
                "shared": ["lr"],
                "left_only": ["optimizer"],
                "right_only": ["momentum"],
            },
            "final_metric_keys": {
                "shared": ["accuracy", "loss"],
                "left_only": [],
                "right_only": [],
            },
            "artifact_keys": {
                "shared": ["model.pkl"],
                "left_only": [],
                "right_only": ["log.txt"],
            },
            "final_metric_stats": {
                "accuracy": {
                    "left": {"min": 0.8, "median": 0.9, "max": 0.95},
                    "right": {"min": 0.85, "median": 0.88, "max": 0.92},
                },
                "loss": {
                    "left": {"min": 0.05, "median": 0.1, "max": 0.15},
                    "right": {"min": 0.03, "median": 0.08, "max": 0.12},
                },
            },
        }

        from jernerics.cli import compare_sweeps as compare_sweeps_cmd

        compare_sweeps_cmd(
            project="my-project",
            left="study-1",
            right="study-2",
            server=mock_trials_server,
        )

        captured = capsys.readouterr()
        assert "study-1" in captured.out
        assert "study-2" in captured.out
        assert "Parameter Keys" in captured.out
        assert "Final Metric Keys" in captured.out
        assert "Artifact Keys" in captured.out
        assert "10" in captured.out
        assert "20" in captured.out
        assert "accuracy" in captured.out
        assert "loss" in captured.out

    def test_compare_sweeps_no_server_url_error(self, capsys, monkeypatch, tmp_path):
        from jernerics.cli import compare_sweeps as compare_sweeps_cmd

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        (tmp_path / "pyproject.toml").write_text("[tool]\n[jernerics]\n")

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            compare_sweeps_cmd(
                project="my-project", left="study-1", right="study-2", server=None
            )

        assert exc_info.value.code == 3

    def test_compare_sweeps_uses_pyproject_project_name(
        self, mock_trials_server, capsys, tmp_path
    ):
        """compare-sweeps uses project name from pyproject.toml."""
        MockTrialsHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 10,
            "left_completed_count": 5,
            "right_trial_count": 20,
            "right_completed_count": 15,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        # Create pyproject.toml with project name
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test-project-from-toml"\n'
        )

        from jernerics.cli import compare_sweeps as compare_sweeps_cmd

        compare_sweeps_cmd(
            project=None, left="study-1", right="study-2", server=mock_trials_server
        )

        captured = capsys.readouterr()
        assert "study-1" in captured.out
        assert "study-2" in captured.out

    def test_compare_sweeps_config_error_without_pyproject(
        self, mock_trials_server, capsys, monkeypatch
    ):
        """compare-sweeps exits CONFIG_ERROR when no pyproject.toml."""
        from jernerics.cli import compare_sweeps as compare_sweeps_cmd

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            compare_sweeps_cmd(
                project=None,
                left="study-1",
                right="study-2",
                server=mock_trials_server,
            )

        assert exc_info.value.code == 3
        captured = capsys.readouterr()
        assert "No pyproject.toml found" in captured.out or "--project" in captured.out

    def test_compare_sweeps_explicit_project_overrides_pyproject(
        self, mock_trials_server, capsys, tmp_path
    ):
        """compare-sweeps uses --project value even when pyproject.toml exists."""
        MockTrialsHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 10,
            "left_completed_count": 5,
            "right_trial_count": 20,
            "right_completed_count": 15,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        # Create pyproject.toml with different project name
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "different-project"\n'
        )

        from jernerics.cli import compare_sweeps as compare_sweeps_cmd

        compare_sweeps_cmd(
            project="explicit-project",
            left="study-1",
            right="study-2",
            server=mock_trials_server,
        )

        captured = capsys.readouterr()
        assert "study-1" in captured.out
        assert "study-2" in captured.out


class TestTrialsCommandOptionalProject:
    def test_trials_uses_pyproject_project_name(
        self, mock_trials_server, capsys, tmp_path
    ):
        """trials uses project name from pyproject.toml when --project omitted."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            }
        ]

        # Create pyproject.toml with project name
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test-project-from-toml"\n'
        )

        from jernerics.cli import trials

        trials(project=None, sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "complete" in captured.out

    def test_trials_config_error_without_pyproject(
        self, mock_trials_server, capsys, monkeypatch
    ):
        """trials exits CONFIG_ERROR when no pyproject.toml and --project omitted."""
        from jernerics.cli import trials

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            trials(project=None, sweep="study-1", server=mock_trials_server)

        assert exc_info.value.code == 3
        captured = capsys.readouterr()
        assert "No pyproject.toml found" in captured.out or "--project" in captured.out

    def test_trials_explicit_project_overrides_pyproject(
        self, mock_trials_server, capsys, tmp_path
    ):
        """trials uses --project value even when pyproject.toml exists."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            }
        ]

        # Create pyproject.toml with different project name
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "different-project"\n'
        )

        from jernerics.cli import trials

        trials(project="explicit-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "complete" in captured.out

    def test_trials_server_resolution_without_pyproject(
        self, mock_trials_server, capsys, monkeypatch
    ):
        """trials resolves server from env even without pyproject.toml."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {},
                "artifact_keys": [],
            }
        ]

        from jernerics.cli import trials

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)
        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", mock_trials_server)

        trials(project="my-project", sweep="study-1", server=None)

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "complete" in captured.out

    def test_trials_with_metrics_filter(self, mock_trials_server, capsys):
        """trials with --metrics shows only selected metric columns."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {"accuracy": 0.95, "loss": 0.05, "f1": 0.87},
                "artifact_keys": [],
            }
        ]

        from jernerics.cli import trials

        trials(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            metrics="accuracy,loss",
        )

        captured = capsys.readouterr()
        assert "ACCURACY" in captured.out
        assert "LOSS" in captured.out
        assert "F1" not in captured.out
        assert "0.95" in captured.out
        assert "0.05" in captured.out

    def test_trials_with_metrics_json_output(self, mock_trials_server, capsys):
        """trials with --metrics and --json includes filtered data from endpoint."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            }
        ]

        from jernerics.cli import trials

        trials(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            metrics="accuracy",
            json_output=True,
        )

        captured = capsys.readouterr()
        assert "trial_id" in captured.out
        assert "final_metrics" in captured.out
        assert "accuracy" in captured.out

    def test_trials_without_metrics_shows_all(self, mock_trials_server, capsys):
        """trials without --metrics shows all metric columns."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {"accuracy": 0.95, "loss": 0.05},
                "artifact_keys": [],
            }
        ]

        from jernerics.cli import trials

        trials(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "ACCURACY" in captured.out
        assert "LOSS" in captured.out


class MockHealthHandler(BaseHTTPRequestHandler):
    """Mock HTTP server handler for health testing."""

    response_data: ClassVar[dict] = {}

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        if self.path == "/api/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response_body = json.dumps(MockHealthHandler.response_data).encode("utf-8")
            self.wfile.write(response_body)
        else:
            self.send_error(404, "Not Found")


@pytest.fixture
def mock_health_server():
    """Start a mock HTTP server for health testing."""
    server = HTTPServer(("localhost", 0), MockHealthHandler)
    port = server.server_address[1]

    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://localhost:{port}"

    server.shutdown()


class TestTrackingHealthCommand:
    def test_tracking_health_with_server_option(self, mock_health_server, capsys):
        MockHealthHandler.response_data = {"ok": True}

        from jernerics.cli import tracking_health

        tracking_health(server=mock_health_server)

        captured = capsys.readouterr()
        assert "ok" in captured.out or "OK" in captured.out

    def test_tracking_health_with_env_var(
        self, mock_health_server, capsys, monkeypatch
    ):
        MockHealthHandler.response_data = {"ok": True}

        from jernerics.cli import tracking_health

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", mock_health_server)
        tracking_health(server=None)

        captured = capsys.readouterr()
        assert "ok" in captured.out or "OK" in captured.out

    def test_tracking_health_with_json_flag(self, mock_health_server, capsys):
        MockHealthHandler.response_data = {"ok": True}

        from jernerics.cli import tracking_health

        tracking_health(server=mock_health_server, json_output=True)

        captured = capsys.readouterr()
        assert "ok" in captured.out

    def test_tracking_health_server_url_priority(
        self, mock_health_server, capsys, monkeypatch
    ):
        MockHealthHandler.response_data = {"ok": True}

        from jernerics.cli import tracking_health

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", "http://wrong-url:9999")
        tracking_health(server=mock_health_server)

        captured = capsys.readouterr()
        assert "ok" in captured.out or "OK" in captured.out

    def test_tracking_health_no_server_url_error(self, capsys, monkeypatch, tmp_path):
        from jernerics.cli import tracking_health

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        (tmp_path / "pyproject.toml").write_text("[tool]\n[jernerics]\n")

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            tracking_health(server=None)

        assert exc_info.value.code == 3


class MockMetricHistoryHandler(BaseHTTPRequestHandler):
    """Mock HTTP server handler for metric-history testing."""

    response_data: ClassVar[list[dict]] = []

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/metrics"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response_body = json.dumps(MockMetricHistoryHandler.response_data).encode(
                "utf-8"
            )
            self.wfile.write(response_body)
        else:
            self.send_error(404, "Not Found")


@pytest.fixture
def mock_metric_history_server():
    """Start a mock HTTP server for metric-history testing."""
    server = HTTPServer(("localhost", 0), MockMetricHistoryHandler)
    port = server.server_address[1]

    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://localhost:{port}"

    server.shutdown()


class TestMetricHistoryCommand:
    def test_metric_history_with_server_option(
        self, mock_metric_history_server, capsys
    ):
        MockMetricHistoryHandler.response_data = [
            {
                "trial_id": 1,
                "step": 0,
                "value": 0.85,
                "timestamp_ns": 1705325400000000000,
            },
            {
                "trial_id": 1,
                "step": 1,
                "value": 0.90,
                "timestamp_ns": 1705325405000000000,
            },
        ]

        from jernerics.cli import metric_history

        metric_history(
            sweep="study-1", metric="accuracy", server=mock_metric_history_server
        )

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "0" in captured.out
        assert "0.85" in captured.out
        assert "0.9" in captured.out
        assert "1705325400000000000" in captured.out

    def test_metric_history_with_env_var(
        self, mock_metric_history_server, capsys, monkeypatch
    ):
        MockMetricHistoryHandler.response_data = []

        from jernerics.cli import metric_history

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", mock_metric_history_server)
        metric_history(
            project="my-project", sweep="study-1", metric="accuracy", server=None
        )

        captured = capsys.readouterr()
        assert "No metric history found" in captured.out

    def test_metric_history_with_json_flag(self, mock_metric_history_server, capsys):
        MockMetricHistoryHandler.response_data = [
            {
                "trial_id": 1,
                "step": 0,
                "value": 0.85,
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import metric_history

        metric_history(
            sweep="study-1",
            metric="accuracy",
            server=mock_metric_history_server,
            json_output=True,
        )

        captured = capsys.readouterr()
        assert "trial_id" in captured.out
        assert "step" in captured.out
        assert "value" in captured.out
        assert "timestamp_ns" in captured.out

    def test_metric_history_with_empty_response(
        self, mock_metric_history_server, capsys
    ):
        MockMetricHistoryHandler.response_data = []

        from jernerics.cli import metric_history

        metric_history(
            sweep="study-1", metric="accuracy", server=mock_metric_history_server
        )

        captured = capsys.readouterr()
        assert "No metric history found" in captured.out

    def test_metric_history_displays_rich_table(
        self, mock_metric_history_server, capsys
    ):
        MockMetricHistoryHandler.response_data = [
            {
                "trial_id": 1,
                "step": 0,
                "value": 0.85,
                "timestamp_ns": 1705325400000000000,
            },
            {
                "trial_id": 2,
                "step": 0,
                "value": 0.88,
                "timestamp_ns": 1705325402000000000,
            },
        ]

        from jernerics.cli import metric_history

        metric_history(
            sweep="study-1", metric="accuracy", server=mock_metric_history_server
        )

        captured = capsys.readouterr()
        assert "TRIAL_ID" in captured.out
        assert "STEP" in captured.out
        assert "VALUE" in captured.out
        assert "TIMESTAMP_NS" in captured.out

    def test_metric_history_server_url_priority(
        self, mock_metric_history_server, capsys, monkeypatch
    ):
        MockMetricHistoryHandler.response_data = []

        from jernerics.cli import metric_history

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", "http://wrong-url:9999")
        metric_history(
            sweep="study-1", metric="accuracy", server=mock_metric_history_server
        )

        captured = capsys.readouterr()
        assert "No metric history found" in captured.out

    def test_metric_history_no_server_url_error(self, capsys, monkeypatch, tmp_path):
        from jernerics.cli import metric_history

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        (tmp_path / "pyproject.toml").write_text("[tool]\n[jernerics]\n")

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            metric_history(sweep="study-1", metric="accuracy", server=None)

        assert exc_info.value.code == 3

    def test_metric_history_uses_pyproject_project_name(
        self, mock_metric_history_server, capsys, tmp_path
    ):
        """metric-history uses project name from pyproject.toml."""
        MockMetricHistoryHandler.response_data = [
            {
                "trial_id": 1,
                "step": 0,
                "value": 0.85,
                "timestamp_ns": 1705325400000000000,
            }
        ]

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test-project-from-toml"\n'
        )

        from jernerics.cli import metric_history

        metric_history(
            project=None,
            sweep="study-1",
            metric="accuracy",
            server=mock_metric_history_server,
        )

        captured = capsys.readouterr()
        assert "1" in captured.out

    def test_metric_history_config_error_without_pyproject(
        self, mock_metric_history_server, capsys, monkeypatch
    ):
        """metric-history exits CONFIG_ERROR when no pyproject.toml."""
        from jernerics.cli import metric_history

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            metric_history(
                project=None,
                sweep="study-1",
                metric="accuracy",
                server=mock_metric_history_server,
            )

        assert exc_info.value.code == 3
        captured = capsys.readouterr()
        assert "No pyproject.toml found" in captured.out or "--project" in captured.out

    def test_metric_history_explicit_project_overrides_pyproject(
        self, mock_metric_history_server, capsys, tmp_path
    ):
        """metric-history uses --project value even when pyproject.toml exists."""
        MockMetricHistoryHandler.response_data = [
            {
                "trial_id": 1,
                "step": 0,
                "value": 0.85,
                "timestamp_ns": 1705325400000000000,
            }
        ]

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "different-project"\n'
        )

        from jernerics.cli import metric_history

        metric_history(
            project="explicit-project",
            sweep="study-1",
            metric="accuracy",
            server=mock_metric_history_server,
        )

        captured = capsys.readouterr()
        assert "1" in captured.out


class TestArtifactsCommand:
    def test_artifacts_with_server_option(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "model",
                "filename": "model.pkl",
                "timestamp_ns": 1705325400000000000,
            },
            {
                "trial_id": 2,
                "key": "log",
                "filename": "log.txt",
                "timestamp_ns": 1705325405000000000,
            },
        ]

        from jernerics.cli import artifacts

        artifacts(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "2" in captured.out
        assert "model" in captured.out
        assert "log" in captured.out
        assert "model.pkl" in captured.out
        assert "log.txt" in captured.out
        assert "TRIAL_ID" in captured.out
        assert "KEY" in captured.out
        assert "FILENAME" in captured.out
        assert "TIMESTAMP_NS" in captured.out

    def test_artifacts_with_trial_id(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "model",
                "filename": "model.pkl",
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import artifacts

        artifacts(
            project="my-project", sweep="study-1", trial_id=1, server=mock_trials_server
        )

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "model" in captured.out

    def test_artifacts_with_json_flag(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "model",
                "filename": "model.pkl",
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import artifacts

        artifacts(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            json_output=True,
        )

        captured = capsys.readouterr()
        assert "trial_id" in captured.out
        assert "key" in captured.out
        assert "filename" in captured.out
        assert "timestamp_ns" in captured.out

    def test_artifacts_with_empty_response(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = []

        from jernerics.cli import artifacts

        artifacts(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "No artifacts found" in captured.out

    def test_artifacts_server_url_priority(
        self, mock_trials_server, capsys, monkeypatch
    ):
        MockTrialsHandler.response_data = []

        from jernerics.cli import artifacts

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", "http://wrong-url:9999")
        artifacts(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "No artifacts found" in captured.out

    def test_artifacts_no_server_url_error(self, capsys, monkeypatch, tmp_path):
        from jernerics.cli import artifacts

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        (tmp_path / "pyproject.toml").write_text("[tool]\n[jernerics]\n")

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            artifacts(project="my-project", sweep="study-1", server=None)

        assert exc_info.value.code == 3

    def test_artifacts_displays_rich_table(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "model",
                "filename": "model.pkl",
                "timestamp_ns": 1705325400000000000,
            },
            {
                "trial_id": 1,
                "key": "log",
                "filename": "log.txt",
                "timestamp_ns": 1705325401000000000,
            },
            {
                "trial_id": 2,
                "key": "model",
                "filename": "model.pkl",
                "timestamp_ns": 1705325402000000000,
            },
        ]

        from jernerics.cli import artifacts

        artifacts(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "model" in captured.out
        assert "log" in captured.out
        assert "model.pkl" in captured.out
        assert "log.txt" in captured.out
        assert "TRIAL_ID" in captured.out
        assert "KEY" in captured.out
        assert "FILENAME" in captured.out
        assert "TIMESTAMP_NS" in captured.out

    def test_artifacts_uses_pyproject_project_name(
        self, mock_trials_server, capsys, tmp_path
    ):
        """artifacts uses project name from pyproject.toml when --project omitted."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "model",
                "filename": "model.pkl",
                "timestamp_ns": 1705325400000000000,
            }
        ]

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test-project-from-toml"\n'
        )

        from jernerics.cli import artifacts

        artifacts(project=None, sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "model" in captured.out

    def test_artifacts_config_error_without_pyproject(
        self, mock_trials_server, capsys, monkeypatch
    ):
        """artifacts exits CONFIG_ERROR when no pyproject.toml."""
        from jernerics.cli import artifacts

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            artifacts(project=None, sweep="study-1", server=mock_trials_server)

        assert exc_info.value.code == 3
        captured = capsys.readouterr()
        assert "No pyproject.toml found" in captured.out or "--project" in captured.out

    def test_artifacts_explicit_project_overrides_pyproject(
        self, mock_trials_server, capsys, tmp_path
    ):
        """artifacts uses --project value even when pyproject.toml exists."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "model",
                "filename": "model.pkl",
                "timestamp_ns": 1705325400000000000,
            }
        ]

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "different-project"\n'
        )

        from jernerics.cli import artifacts

        artifacts(
            project="explicit-project", sweep="study-1", server=mock_trials_server
        )

        captured = capsys.readouterr()
        assert "model" in captured.out


class TestResultsCommand:
    def test_results_with_server_option(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            },
            {
                "trial_id": 2,
                "key": "accuracy",
                "value": "0.95",
                "timestamp_ns": 1705325405000000000,
            },
        ]

        from jernerics.cli import results

        results(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "2" in captured.out
        assert "confusion" in captured.out
        assert "accuracy" in captured.out
        assert '{"tp": 90}' in captured.out
        assert "0.95" in captured.out
        assert "TRIAL_ID" in captured.out
        assert "KEY" in captured.out
        assert "VALUE" in captured.out
        assert "TIMESTAMP_NS" in captured.out

    def test_results_with_trial_id(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import results

        results(
            project="my-project", sweep="study-1", trial_id=1, server=mock_trials_server
        )

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "confusion" in captured.out

    def test_results_with_key(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import results

        results(
            project="my-project",
            sweep="study-1",
            key="confusion",
            server=mock_trials_server,
        )

        captured = capsys.readouterr()
        assert "confusion" in captured.out
        assert '{"tp": 90}' in captured.out

    def test_results_with_json_flag(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import results

        results(
            project="my-project",
            sweep="study-1",
            server=mock_trials_server,
            json_output=True,
        )

        captured = capsys.readouterr()
        assert "trial_id" in captured.out
        assert "key" in captured.out
        assert "value" in captured.out
        assert "timestamp_ns" in captured.out
        # JSON output will have escaped quotes
        assert '"{\\"tp\\": 90}"' in captured.out or '{"tp": 90}' in captured.out

    def test_results_with_empty_response(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = []

        from jernerics.cli import results

        results(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "No results found" in captured.out

    def test_results_server_url_priority(self, mock_trials_server, capsys, monkeypatch):
        MockTrialsHandler.response_data = []

        from jernerics.cli import results

        monkeypatch.setenv("JERNERICS_TRACKING_HTTP_SERVER", "http://wrong-url:9999")
        results(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "No results found" in captured.out

    def test_results_no_server_url_error(self, capsys, monkeypatch, tmp_path):
        from jernerics.cli import results

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        (tmp_path / "pyproject.toml").write_text("[tool]\n[jernerics]\n")

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=tmp_path),
            pytest.raises(SystemExit) as exc_info,
        ):
            results(project="my-project", sweep="study-1", server=None)

        assert exc_info.value.code == 3

    def test_results_displays_rich_table(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            },
            {
                "trial_id": 1,
                "key": "accuracy",
                "value": "0.95",
                "timestamp_ns": 1705325401000000000,
            },
            {
                "trial_id": 2,
                "key": "confusion",
                "value": '{"tp": 85}',
                "timestamp_ns": 1705325402000000000,
            },
        ]

        from jernerics.cli import results

        results(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "confusion" in captured.out
        assert "accuracy" in captured.out
        assert '{"tp": 90}' in captured.out
        assert '{"tp": 85}' in captured.out
        assert "0.95" in captured.out
        assert "TRIAL_ID" in captured.out
        assert "KEY" in captured.out
        assert "VALUE" in captured.out
        assert "TIMESTAMP_NS" in captured.out

    def test_results_uses_pyproject_project_name(
        self, mock_trials_server, capsys, tmp_path
    ):
        """results uses project name from pyproject.toml when --project omitted."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            }
        ]

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "test-project-from-toml"\n'
        )

        from jernerics.cli import results

        results(project=None, sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "confusion" in captured.out

    def test_results_config_error_without_pyproject(
        self, mock_trials_server, capsys, monkeypatch
    ):
        """results exits CONFIG_ERROR when no pyproject.toml."""
        from jernerics.cli import results

        monkeypatch.delenv("JERNERICS_TRACKING_HTTP_SERVER", raising=False)

        with (
            patch("jernerics.cli.find_pyproject_dir", return_value=None),
            pytest.raises(SystemExit) as exc_info,
        ):
            results(project=None, sweep="study-1", server=mock_trials_server)

        assert exc_info.value.code == 3
        captured = capsys.readouterr()
        assert "No pyproject.toml found" in captured.out or "--project" in captured.out

    def test_results_explicit_project_overrides_pyproject(
        self, mock_trials_server, capsys, tmp_path
    ):
        """results uses --project value even when pyproject.toml exists."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            }
        ]

        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "different-project"\n'
        )

        from jernerics.cli import results

        results(project="explicit-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "confusion" in captured.out

    def test_results_value_is_string_not_parsed(self, mock_trials_server, capsys):
        """results command returns value as stored string, not parsed."""
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90, "fp": 10, "tn": 80, "fn": 20}',
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import results

        results(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        # Value should be the JSON string, parts of it appear in the table
        assert '"tp": 90' in captured.out or "tp" in captured.out
        assert '"fp": 10' in captured.out or "fp" in captured.out

    def test_results_with_trial_id_and_key(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            }
        ]

        from jernerics.cli import results

        results(
            project="my-project",
            sweep="study-1",
            trial_id=1,
            key="confusion",
            server=mock_trials_server,
        )

        captured = capsys.readouterr()
        assert "1" in captured.out
        assert "confusion" in captured.out
        assert '{"tp": 90}' in captured.out

    def test_results_displays_multiple_results(self, mock_trials_server, capsys):
        MockTrialsHandler.response_data = [
            {
                "trial_id": 1,
                "key": "confusion",
                "value": '{"tp": 90}',
                "timestamp_ns": 1705325400000000000,
            },
            {
                "trial_id": 1,
                "key": "accuracy",
                "value": "0.95",
                "timestamp_ns": 1705325401000000000,
            },
            {
                "trial_id": 2,
                "key": "confusion",
                "value": '{"tp": 85}',
                "timestamp_ns": 1705325402000000000,
            },
            {
                "trial_id": 2,
                "key": "accuracy",
                "value": "0.92",
                "timestamp_ns": 1705325403000000000,
            },
        ]

        from jernerics.cli import results

        results(project="my-project", sweep="study-1", server=mock_trials_server)

        captured = capsys.readouterr()
        assert "confusion" in captured.out
        assert "accuracy" in captured.out
        assert "0.95" in captured.out
        assert "0.92" in captured.out
        assert '{"tp": 90}' in captured.out
        assert '{"tp": 85}' in captured.out
