import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import ClassVar

import pytest
from jernerics.tracking.http_api import compare_sweeps, list_sweeps, list_trials


class MockHandler(BaseHTTPRequestHandler):
    """Mock HTTP server handler for testing."""

    received_headers: ClassVar[dict] = {}
    received_path: ClassVar[str] = ""
    response_data: ClassVar[list[dict] | dict] = []
    response_status: ClassVar[int] = 200
    return_invalid_json: ClassVar[bool] = False
    response_body: ClassVar[bytes] = b""

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        if (
            self.path == "/api/sweeps"
            or self.path.startswith("/api/sweeps?")
            or self.path.startswith("/api/trials")
            or self.path.startswith("/api/compare-sweeps")
            or self.path == "/api/health"
        ):
            MockHandler.received_headers = dict(self.headers)
            MockHandler.received_path = self.path

            if MockHandler.response_status != 200:
                self.send_response(MockHandler.response_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if MockHandler.response_body:
                    self.wfile.write(MockHandler.response_body)
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if MockHandler.return_invalid_json:
                self.wfile.write(b"invalid json")
            else:
                response_body = json.dumps(MockHandler.response_data).encode("utf-8")
                self.wfile.write(response_body)
        else:
            self.send_error(404, "Not Found")


@pytest.fixture
def mock_server():
    """Start a mock HTTP server for testing."""
    server = HTTPServer(("localhost", 0), MockHandler)
    port = server.server_address[1]

    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://localhost:{port}"

    server.shutdown()


class TestListSweeps:
    def test_list_sweeps_success(self, mock_server):
        MockHandler.response_data = [
            {
                "project": "my-project",
                "study_name": "study-1",
                "trials": 10,
                "completed": 5,
                "last_event": "2024-01-15T10:30:00Z",
            }
        ]

        result = list_sweeps(mock_server)

        assert len(result) == 1
        assert result[0]["project"] == "my-project"
        assert result[0]["study_name"] == "study-1"
        assert result[0]["trials"] == 10
        assert result[0]["completed"] == 5
        assert result[0]["last_event"] == "2024-01-15T10:30:00Z"

    def test_list_sweeps_multiple_items(self, mock_server):
        MockHandler.response_data = [
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

        result = list_sweeps(mock_server)

        assert len(result) == 2
        assert result[0]["project"] == "project-a"
        assert result[1]["project"] == "project-b"

    def test_list_sweeps_empty_list(self, mock_server):
        MockHandler.response_data = []

        result = list_sweeps(mock_server)

        assert result == []

    def test_list_sweeps_with_api_key(self, mock_server):
        MockHandler.response_data = []

        os.environ["JERNERICS_API_KEY"] = "test-key-123"
        try:
            list_sweeps(mock_server)
        finally:
            os.environ.pop("JERNERICS_API_KEY", None)

        assert "Authorization" in MockHandler.received_headers
        assert MockHandler.received_headers["Authorization"] == "Bearer test-key-123"

    def test_list_sweeps_without_api_key(self, mock_server):
        MockHandler.response_data = []

        api_key = os.environ.pop("JERNERICS_API_KEY", None)
        try:
            list_sweeps(mock_server)
        finally:
            if api_key:
                os.environ["JERNERICS_API_KEY"] = api_key

        assert "Authorization" not in MockHandler.received_headers

    def test_list_sweeps_accepts_trailing_slash(self, mock_server):
        MockHandler.response_data = []

        result = list_sweeps(mock_server + "/")

        assert result == []

    def test_list_sweeps_http_error(self, mock_server):
        MockHandler.response_status = 500
        MockHandler.response_data = []

        with pytest.raises(RuntimeError) as exc_info:
            list_sweeps(mock_server)

        assert "500" in str(exc_info.value)
        MockHandler.response_status = 200

    def test_list_sweeps_unreachable_server(self):
        with pytest.raises(RuntimeError) as exc_info:
            list_sweeps("http://localhost:9999")

        assert "http://localhost:9999" in str(exc_info.value)

    def test_list_sweeps_invalid_json(self, mock_server):
        MockHandler.response_data = []  # Override the response
        MockHandler.response_status = 200
        MockHandler.return_invalid_json = True

        with pytest.raises(RuntimeError, match="invalid JSON"):
            list_sweeps(mock_server)

        MockHandler.return_invalid_json = False

    def test_list_sweeps_http_error_with_detail(self, mock_server):
        MockHandler.response_status = 403
        body = json.dumps({"detail": "Forbidden access"})
        MockHandler.response_body = body.encode("utf-8")

        with pytest.raises(RuntimeError) as exc_info:
            list_sweeps(mock_server)

        assert "403" in str(exc_info.value)
        assert "Forbidden access" in str(exc_info.value)

        MockHandler.response_status = 200
        MockHandler.response_body = b""

    def test_list_sweeps_http_error_with_error_field(self, mock_server):
        MockHandler.response_status = 500
        body = json.dumps({"error": "Internal server error"})
        MockHandler.response_body = body.encode("utf-8")

        with pytest.raises(RuntimeError) as exc_info:
            list_sweeps(mock_server)

        assert "500" in str(exc_info.value)
        assert "Internal server error" in str(exc_info.value)

        MockHandler.response_status = 200
        MockHandler.response_body = b""

    def test_list_sweeps_http_error_no_body(self, mock_server):
        MockHandler.response_status = 503
        MockHandler.response_body = b""

        with pytest.raises(RuntimeError) as exc_info:
            list_sweeps(mock_server)

        assert "503" in str(exc_info.value)

        MockHandler.response_status = 200

    def test_list_sweeps_sends_accept_header(self, mock_server):
        MockHandler.response_data = []

        list_sweeps(mock_server)

        assert "Accept" in MockHandler.received_headers
        assert MockHandler.received_headers["Accept"] == "application/json"

    def test_list_sweeps_with_project(self, mock_server):
        MockHandler.response_data = [
            {
                "project": "my-project",
                "study_name": "study-1",
                "trial_count": 10,
                "completed_count": 5,
                "last_event_timestamp_ns": 1705325400000000000,
            }
        ]

        result = list_sweeps(mock_server, project="my-project")

        assert len(result) == 1
        assert result[0]["project"] == "my-project"
        assert result[0]["study_name"] == "study-1"
        assert "project=my-project" in MockHandler.received_path

    def test_list_sweeps_project_url_encodes_spaces(self, mock_server):
        MockHandler.response_data = []

        result = list_sweeps(mock_server, project="my project")

        assert result == []
        assert "project=my+project" in MockHandler.received_path

    def test_list_sweeps_project_url_encodes_special_chars(self, mock_server):
        MockHandler.response_data = []

        result = list_sweeps(mock_server, project="project&a")

        assert result == []
        assert "project=project%26a" in MockHandler.received_path


class TestListTrials:
    def test_list_trials_success(self, mock_server):
        MockHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01, "batch_size": 32},
                "final_metrics": {"accuracy": 0.95, "loss": 0.05},
                "artifact_keys": ["model.pkl", "log.txt"],
            }
        ]

        result = list_trials(mock_server, "my-project", "study-1")

        assert len(result) == 1
        assert result[0]["trial_id"] == 1
        assert result[0]["status"] == "complete"
        assert result[0]["params"] == {"lr": 0.01, "batch_size": 32}
        assert result[0]["final_metrics"] == {"accuracy": 0.95, "loss": 0.05}
        assert result[0]["artifact_keys"] == ["model.pkl", "log.txt"]

    def test_list_trials_multiple_items(self, mock_server):
        MockHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {"lr": 0.01},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            },
            {
                "trial_id": 2,
                "status": "incomplete",
                "params": {"lr": 0.001},
                "final_metrics": {},
                "artifact_keys": ["log.txt"],
            },
        ]

        result = list_trials(mock_server, "my-project", "study-1")

        assert len(result) == 2
        assert result[0]["trial_id"] == 1
        assert result[1]["trial_id"] == 2

    def test_list_trials_empty_list(self, mock_server):
        MockHandler.response_data = []

        result = list_trials(mock_server, "my-project", "study-1")

        assert result == []

    def test_list_trials_with_api_key(self, mock_server):
        MockHandler.response_data = []

        os.environ["JERNERICS_API_KEY"] = "test-key-123"
        try:
            list_trials(mock_server, "my-project", "study-1")
        finally:
            os.environ.pop("JERNERICS_API_KEY", None)

        assert "Authorization" in MockHandler.received_headers
        assert MockHandler.received_headers["Authorization"] == "Bearer test-key-123"

    def test_list_trials_without_api_key(self, mock_server):
        MockHandler.response_data = []

        api_key = os.environ.pop("JERNERICS_API_KEY", None)
        try:
            list_trials(mock_server, "my-project", "study-1")
        finally:
            if api_key:
                os.environ["JERNERICS_API_KEY"] = api_key

        assert "Authorization" not in MockHandler.received_headers

    def test_list_trials_with_limit(self, mock_server):
        MockHandler.response_data = [
            {
                "trial_id": i,
                "status": "complete",
                "params": {},
                "final_metrics": {},
                "artifact_keys": [],
            }
            for i in range(1, 11)
        ]

        result = list_trials(mock_server, "my-project", "study-1", limit=5)

        assert len(result) == 5
        assert result[0]["trial_id"] == 1
        assert result[4]["trial_id"] == 5

    def test_list_trials_limit_default(self, mock_server):
        MockHandler.response_data = [
            {
                "trial_id": i,
                "status": "complete",
                "params": {},
                "final_metrics": {},
                "artifact_keys": [],
            }
            for i in range(1, 201)
        ]

        result = list_trials(mock_server, "my-project", "study-1")

        assert len(result) == 100

    def test_list_trials_http_error(self, mock_server):
        MockHandler.response_status = 500
        MockHandler.response_data = []

        with pytest.raises(RuntimeError) as exc_info:
            list_trials(mock_server, "my-project", "study-1")

        assert "500" in str(exc_info.value)
        MockHandler.response_status = 200

    def test_list_trials_unreachable_server(self):
        with pytest.raises(RuntimeError) as exc_info:
            list_trials("http://localhost:9999", "my-project", "study-1")

        assert "http://localhost:9999" in str(exc_info.value)

    def test_list_trials_invalid_json(self, mock_server):
        MockHandler.response_data = []
        MockHandler.response_status = 200
        MockHandler.return_invalid_json = True

        with pytest.raises(RuntimeError, match="invalid JSON"):
            list_trials(mock_server, "my-project", "study-1")

        MockHandler.return_invalid_json = False

    def test_list_trials_sends_accept_header(self, mock_server):
        MockHandler.response_data = []

        list_trials(mock_server, "my-project", "study-1")

        assert "Accept" in MockHandler.received_headers
        assert MockHandler.received_headers["Accept"] == "application/json"

    def test_list_trials_url_encodes_spaces(self, mock_server):
        MockHandler.response_data = []

        list_trials(mock_server, "my project", "study one")

        assert "project=my+project" in MockHandler.received_path
        assert "study_name=study+one" in MockHandler.received_path

    def test_list_trials_url_encodes_ampersands(self, mock_server):
        MockHandler.response_data = []

        list_trials(mock_server, "project&a", "study&b")

        assert "project=project%26a" in MockHandler.received_path
        assert "study_name=study%26b" in MockHandler.received_path

    def test_list_trials_with_metric_keys(self, mock_server):
        MockHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {"accuracy": 0.95},
                "artifact_keys": [],
            }
        ]

        result = list_trials(
            mock_server, "my-project", "study-1", metric_keys="accuracy"
        )

        assert len(result) == 1
        assert result[0]["final_metrics"] == {"accuracy": 0.95}
        assert "metric_keys=accuracy" in MockHandler.received_path

    def test_list_trials_without_metric_keys(self, mock_server):
        MockHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {"accuracy": 0.95, "loss": 0.05},
                "artifact_keys": [],
            }
        ]

        result = list_trials(mock_server, "my-project", "study-1")

        assert len(result) == 1
        assert result[0]["final_metrics"] == {"accuracy": 0.95, "loss": 0.05}
        assert "metric_keys" not in MockHandler.received_path

    def test_list_trials_with_multiple_metric_keys(self, mock_server):
        MockHandler.response_data = [
            {
                "trial_id": 1,
                "status": "complete",
                "params": {},
                "final_metrics": {"accuracy": 0.95, "loss": 0.05},
                "artifact_keys": [],
            }
        ]

        result = list_trials(
            mock_server, "my-project", "study-1", metric_keys="accuracy,loss"
        )

        assert len(result) == 1
        assert result[0]["final_metrics"] == {"accuracy": 0.95, "loss": 0.05}
        assert "metric_keys=accuracy%2Closs" in MockHandler.received_path


class TestCompareSweeps:
    def test_compare_sweeps_success(self, mock_server):
        MockHandler.response_data = {
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

        result = compare_sweeps(mock_server, "my-project", "study-1", "study-2")

        assert result["left"] == "study-1"
        assert result["right"] == "study-2"
        assert result["left_trial_count"] == 10
        assert result["left_completed_count"] == 5
        assert result["right_trial_count"] == 20
        assert result["right_completed_count"] == 15
        assert result["param_keys"]["shared"] == ["lr", "batch_size"]
        assert result["final_metric_keys"]["shared"] == ["accuracy"]
        assert result["artifact_keys"]["shared"] == ["model.pkl"]
        assert "accuracy" in result["final_metric_stats"]

    def test_compare_sweeps_with_api_key(self, mock_server):
        MockHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 0,
            "left_completed_count": 0,
            "right_trial_count": 0,
            "right_completed_count": 0,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        os.environ["JERNERICS_API_KEY"] = "test-key-123"
        try:
            compare_sweeps(mock_server, "my-project", "study-1", "study-2")
        finally:
            os.environ.pop("JERNERICS_API_KEY", None)

        assert "Authorization" in MockHandler.received_headers
        assert MockHandler.received_headers["Authorization"] == "Bearer test-key-123"

    def test_compare_sweeps_without_api_key(self, mock_server):
        MockHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 0,
            "left_completed_count": 0,
            "right_trial_count": 0,
            "right_completed_count": 0,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        api_key = os.environ.pop("JERNERICS_API_KEY", None)
        try:
            compare_sweeps(mock_server, "my-project", "study-1", "study-2")
        finally:
            if api_key:
                os.environ["JERNERICS_API_KEY"] = api_key

        assert "Authorization" not in MockHandler.received_headers

    def test_compare_sweeps_http_error(self, mock_server):
        MockHandler.response_status = 404
        MockHandler.response_data = {}

        with pytest.raises(RuntimeError) as exc_info:
            compare_sweeps(mock_server, "my-project", "study-1", "study-2")

        assert "404" in str(exc_info.value)
        MockHandler.response_status = 200

    def test_compare_sweeps_unreachable_server(self):
        with pytest.raises(RuntimeError) as exc_info:
            compare_sweeps("http://localhost:9999", "my-project", "study-1", "study-2")

        assert "http://localhost:9999" in str(exc_info.value)

    def test_compare_sweeps_invalid_json(self, mock_server):
        MockHandler.response_data = {}
        MockHandler.response_status = 200
        MockHandler.return_invalid_json = True

        with pytest.raises(RuntimeError, match="invalid JSON"):
            compare_sweeps(mock_server, "my-project", "study-1", "study-2")

        MockHandler.return_invalid_json = False

    def test_compare_sweeps_sends_accept_header(self, mock_server):
        MockHandler.response_data = {
            "left": "study-1",
            "right": "study-2",
            "left_trial_count": 0,
            "left_completed_count": 0,
            "right_trial_count": 0,
            "right_completed_count": 0,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        compare_sweeps(mock_server, "my-project", "study-1", "study-2")

        assert "Accept" in MockHandler.received_headers
        assert MockHandler.received_headers["Accept"] == "application/json"

    def test_compare_sweeps_url_encodes_spaces(self, mock_server):
        MockHandler.response_data = {
            "left": "study one",
            "right": "study two",
            "left_trial_count": 0,
            "left_completed_count": 0,
            "right_trial_count": 0,
            "right_completed_count": 0,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        compare_sweeps(mock_server, "my project", "study one", "study two")

        assert "project=my+project" in MockHandler.received_path
        assert "left=study+one" in MockHandler.received_path
        assert "right=study+two" in MockHandler.received_path

    def test_compare_sweeps_url_encodes_ampersands(self, mock_server):
        MockHandler.response_data = {
            "left": "study&a",
            "right": "study&b",
            "left_trial_count": 0,
            "left_completed_count": 0,
            "right_trial_count": 0,
            "right_completed_count": 0,
            "param_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_keys": {"shared": [], "left_only": [], "right_only": []},
            "artifact_keys": {"shared": [], "left_only": [], "right_only": []},
            "final_metric_stats": {},
        }

        compare_sweeps(mock_server, "project&a", "study&a", "study&b")

        assert "project=project%26a" in MockHandler.received_path
        assert "left=study%26a" in MockHandler.received_path
        assert "right=study%26b" in MockHandler.received_path


class TestGetHealth:
    def test_get_health_success(self, mock_server):
        MockHandler.response_data = {"ok": True}

        from jernerics.tracking.http_api import get_health

        result = get_health(mock_server)

        assert result == {"ok": True}

    def test_get_health_with_api_key(self, mock_server):
        MockHandler.response_data = {"ok": True}

        os.environ["JERNERICS_API_KEY"] = "test-key-123"
        try:
            from jernerics.tracking.http_api import get_health

            get_health(mock_server)
        finally:
            os.environ.pop("JERNERICS_API_KEY", None)

        assert "Authorization" in MockHandler.received_headers
        assert MockHandler.received_headers["Authorization"] == "Bearer test-key-123"

    def test_get_health_without_api_key(self, mock_server):
        MockHandler.response_data = {"ok": True}

        api_key = os.environ.pop("JERNERICS_API_KEY", None)
        try:
            from jernerics.tracking.http_api import get_health

            get_health(mock_server)
        finally:
            if api_key:
                os.environ["JERNERICS_API_KEY"] = api_key

        assert "Authorization" not in MockHandler.received_headers

    def test_get_health_accepts_trailing_slash(self, mock_server):
        MockHandler.response_data = {"ok": True}

        from jernerics.tracking.http_api import get_health

        result = get_health(mock_server + "/")

        assert result == {"ok": True}

    def test_get_health_http_error(self, mock_server):
        MockHandler.response_status = 500
        MockHandler.response_data = {}

        from jernerics.tracking.http_api import get_health

        with pytest.raises(RuntimeError) as exc_info:
            get_health(mock_server)

        assert "500" in str(exc_info.value)
        MockHandler.response_status = 200

    def test_get_health_unreachable_server(self):
        from jernerics.tracking.http_api import get_health

        with pytest.raises(RuntimeError) as exc_info:
            get_health("http://localhost:9999")

        assert "http://localhost:9999" in str(exc_info.value)

    def test_get_health_invalid_json(self, mock_server):
        MockHandler.response_data = {}
        MockHandler.response_status = 200
        MockHandler.return_invalid_json = True

        from jernerics.tracking.http_api import get_health

        with pytest.raises(RuntimeError, match="invalid JSON"):
            get_health(mock_server)

        MockHandler.return_invalid_json = False

    def test_get_health_http_error_with_detail(self, mock_server):
        MockHandler.response_status = 503
        body = json.dumps({"detail": "Service unavailable"})
        MockHandler.response_body = body.encode("utf-8")

        from jernerics.tracking.http_api import get_health

        with pytest.raises(RuntimeError) as exc_info:
            get_health(mock_server)

        assert "503" in str(exc_info.value)
        assert "Service unavailable" in str(exc_info.value)

        MockHandler.response_status = 200
        MockHandler.response_body = b""

    def test_get_health_sends_accept_header(self, mock_server):
        MockHandler.response_data = {"ok": True}

        from jernerics.tracking.http_api import get_health

        get_health(mock_server)

        assert "Accept" in MockHandler.received_headers
        assert MockHandler.received_headers["Accept"] == "application/json"
