import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import ClassVar
from urllib.error import HTTPError, URLError

import pytest
from jernerics.tracking.http_api import list_sweeps


class MockHandler(BaseHTTPRequestHandler):
    """Mock HTTP server handler for testing."""

    received_headers: ClassVar[dict] = {}
    response_data: ClassVar[list[dict]] = []
    response_status: ClassVar[int] = 200
    return_invalid_json: ClassVar[bool] = False

    def log_message(self, format: str, *args):
        pass

    def do_GET(self):
        if self.path != "/api/sweeps":
            self.send_error(404, "Not Found")
            return

        MockHandler.received_headers = dict(self.headers)

        if MockHandler.response_status != 200:
            self.send_response(MockHandler.response_status)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if MockHandler.return_invalid_json:
            self.wfile.write(b"invalid json")
        else:
            response_body = json.dumps(MockHandler.response_data).encode("utf-8")
            self.wfile.write(response_body)


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

        with pytest.raises(HTTPError) as exc_info:
            list_sweeps(mock_server)

        assert exc_info.value.code == 500
        MockHandler.response_status = 200

    def test_list_sweeps_unreachable_server(self):
        with pytest.raises(URLError):
            list_sweeps("http://localhost:9999")

    def test_list_sweeps_invalid_json(self, mock_server):
        MockHandler.response_data = []  # Override the response
        MockHandler.response_status = 200
        MockHandler.return_invalid_json = True

        with pytest.raises(json.JSONDecodeError):
            list_sweeps(mock_server)

        MockHandler.return_invalid_json = False

    def test_list_sweeps_sends_accept_header(self, mock_server):
        MockHandler.response_data = []

        list_sweeps(mock_server)

        assert "Accept" in MockHandler.received_headers
        assert MockHandler.received_headers["Accept"] == "application/json"
