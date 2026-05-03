from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from jernerics_proto import ArtifactEvent, Envelope
from jernerics_server.http import create_app
from jernerics_server.store import DuckDBStore


def _fake_s3_fetch(bucket: str, key: str) -> tuple[BytesIO, str]:
    return BytesIO(b"fake-model-data"), key


@pytest.fixture
def client(tmp_path):
    store = DuckDBStore(tmp_path / "test.duckdb")
    app = create_app(store, s3_fetch=_fake_s3_fetch)
    app.state.store = store
    return TestClient(app)


@pytest.fixture
def auth_client(tmp_path):
    store = DuckDBStore(tmp_path / "test.duckdb")
    app = create_app(store, api_key="secret123", s3_fetch=_fake_s3_fetch)
    app.state.store = store
    return TestClient(app)


class TestQueryEndpoint:
    def test_valid_select_returns_columns_and_rows(self, client):
        response = client.post("/query", json={"sql": "SELECT 1 AS n, 'hello' AS s"})
        assert response.status_code == 200
        body = response.json()
        assert body["columns"] == ["n", "s"]
        assert body["rows"] == [[1, "hello"]]

    def test_rejects_insert(self, client):
        response = client.post(
            "/query", json={"sql": "INSERT INTO params VALUES ('x', 1)"}
        )
        assert response.status_code == 400
        body = response.json()
        assert "error" in body

    def test_rejects_delete(self, client):
        response = client.post("/query", json={"sql": "DELETE FROM params"})
        assert response.status_code == 400

    def test_rejects_drop(self, client):
        response = client.post("/query", json={"sql": "DROP TABLE params"})
        assert response.status_code == 400

    def test_rejects_update(self, client):
        response = client.post("/query", json={"sql": "UPDATE params SET key = 'x'"})
        assert response.status_code == 400

    def test_invalid_sql_returns_error(self, client):
        response = client.post("/query", json={"sql": "SELECTT 1"})
        assert response.status_code == 400
        body = response.json()
        assert "error" in body

    def test_references_nonexistent_table(self, client):
        response = client.post("/query", json={"sql": "SELECT * FROM nonexistent"})
        assert response.status_code == 400
        body = response.json()
        assert "error" in body

    def test_null_serializes_as_json_null(self, client):
        response = client.post("/query", json={"sql": "SELECT NULL AS v"})
        assert response.status_code == 200
        body = response.json()
        assert body["rows"] == [[None]]

    def test_row_limit_enforced(self, client):
        response = client.post(
            "/query", json={"sql": "SELECT * FROM generate_series(1, 11000)"}
        )
        assert response.status_code == 400
        body = response.json()
        assert "error" in body


class TestQueryAuth:
    def test_valid_bearer_passes(self, auth_client):
        response = auth_client.post(
            "/query",
            json={"sql": "SELECT 1 AS n"},
            headers={"Authorization": "Bearer secret123"},
        )
        assert response.status_code == 200

    def test_missing_auth_returns_401(self, auth_client):
        response = auth_client.post(
            "/query",
            json={"sql": "SELECT 1 AS n"},
        )
        assert response.status_code == 401

    def test_invalid_key_returns_401(self, auth_client):
        response = auth_client.post(
            "/query",
            json={"sql": "SELECT 1 AS n"},
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401

    def test_no_auth_when_key_not_set(self, client):
        response = client.post("/query", json={"sql": "SELECT 1 AS n"})
        assert response.status_code == 200


class TestArtifactProxy:
    def test_returns_file_with_content_type(self, client):
        db = client.app.state.store
        env = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            artifact=ArtifactEvent(key="model", filename="model.bin"),
        )
        db.insert_event(env)

        response = client.get("/artifact/myproj/mystudy/0/model")
        assert response.status_code == 200
        assert response.content == b"fake-model-data"
        assert response.headers["content-type"] == "application/octet-stream"

    def test_png_content_type(self, client):
        db = client.app.state.store
        env = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=1000,
            seq=0,
            artifact=ArtifactEvent(key="plot", filename="loss.png"),
        )
        db.insert_event(env)

        response = client.get("/artifact/p/s/1/plot")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"

    def test_404_for_missing_key_in_duckdb(self, client):
        response = client.get("/artifact/nope/nope/0/nope")
        assert response.status_code == 404

    def test_404_for_missing_file_in_s3(self, tmp_path):
        def raise_not_found(bucket, key):
            raise FileNotFoundError("not found")

        store = DuckDBStore(tmp_path / "test.duckdb")
        app = create_app(store, s3_fetch=raise_not_found)
        app.state.store = store
        c = TestClient(app)

        env = Envelope(
            project="p",
            study_name="s",
            trial_id=2,
            timestamp_ns=1000,
            seq=0,
            artifact=ArtifactEvent(key="gone", filename="gone.csv"),
        )
        store.insert_event(env)

        response = c.get("/artifact/p/s/2/gone")
        assert response.status_code == 404

    def test_uses_streaming_response(self, tmp_path):
        class ChunkedBody:
            """File-like that yields chunks, proving streaming."""

            def __init__(self, data: bytes, chunk_size: int = 8):
                self._data = data
                self._chunk_size = chunk_size
                self._pos = 0
                self.read_calls = 0

            def read(self, size: int = -1) -> bytes:
                self.read_calls += 1
                if self._pos >= len(self._data):
                    return b""
                if size == -1:
                    chunk = self._data[self._pos :]
                else:
                    chunk = self._data[self._pos : self._pos + size]
                self._pos += len(chunk)
                return chunk

        chunk_size = 8
        body = ChunkedBody(b"A" * 100, chunk_size=chunk_size)

        def mock_fetch(bucket, key):
            return body, key

        store = DuckDBStore(tmp_path / "test.duckdb")
        app = create_app(store, s3_fetch=mock_fetch)
        app.state.store = store
        c = TestClient(app)

        env = Envelope(
            project="p",
            study_name="s",
            trial_id=3,
            timestamp_ns=1000,
            seq=0,
            artifact=ArtifactEvent(key="big", filename="big.bin"),
        )
        store.insert_event(env)

        response = c.get("/artifact/p/s/3/big")
        assert response.status_code == 200
        assert response.content == b"A" * 100
        # StreamingResponse calls read() with a chunk size, not read() once for all
        assert body.read_calls > 1
