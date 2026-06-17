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
        if self.path == "/api/sweeps" or self.path.startswith("/api/trials"):
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
                "trials": 10,
                "completed": 5,
                "last_event": "2024-01-15T10:30:00Z",
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
                "trials": 10,
                "completed": 5,
                "last_event": "2024-01-15T10:30:00Z",
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
                "trials": 5,
                "completed": 2,
                "last_event": "2024-01-15T10:30:00Z",
            },
            {
                "project": "project-b",
                "study_name": "study-2",
                "trials": 20,
                "completed": 20,
                "last_event": "2024-01-15T11:00:00Z",
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


class MockTrialsHandler(BaseHTTPRequestHandler):
    """Mock HTTP server handler for trials testing."""

    response_data: ClassVar[list[dict]] = []

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/api/trials"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response_body = json.dumps(MockTrialsHandler.response_data).encode("utf-8")
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
