import pytest
from fastapi.testclient import TestClient
from jernerics_server.http import create_app
from jernerics_server.store import Store


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    app = create_app(store)
    app.state.store = store
    return TestClient(app)


@pytest.fixture
def auth_client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    app = create_app(store, api_key="secret123")
    app.state.store = store
    return TestClient(app)


@pytest.fixture
def artifacts_client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    app = create_app(store, artifacts_root=tmp_path / "artifacts")
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
            "/query",
            json={
                "sql": (
                    "WITH RECURSIVE cnt(x) AS ("
                    "SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x < 11000) "
                    "SELECT * FROM cnt"
                ),
            },
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


class TestHealthEndpoint:
    def test_returns_ok_true(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
        assert body == {"ok": True}

    def test_requires_bearer_auth(self, auth_client):
        response = auth_client.get("/api/health")
        assert response.status_code == 401

        response = auth_client.get(
            "/api/health", headers={"Authorization": "Bearer secret123"}
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_no_auth_when_key_not_set(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True}


def _metric_envelope(seq: int = 0) -> dict:
    return {
        "project": "p",
        "study_name": "s",
        "trial_id": 0,
        "timestamp_ns": 1,
        "seq": seq,
        "metric": {"key": "loss", "value": 0.5, "step": 10},
    }


class TestIngestEndpoint:
    def test_metric_round_trips_through_query(self, client):
        response = client.post("/ingest", json=_metric_envelope())
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        q = client.post("/query", json={"sql": "SELECT key, value FROM metrics"})
        assert q.status_code == 200
        body = q.json()
        assert body["columns"] == ["key", "value"]
        assert body["rows"] == [["loss", 0.5]]

    def test_duplicate_seq_is_idempotent(self, client):
        envelope = _metric_envelope()
        assert client.post("/ingest", json=envelope).status_code == 200
        assert client.post("/ingest", json=envelope).status_code == 200

        q = client.post("/query", json={"sql": "SELECT COUNT(*) FROM metrics"})
        assert q.json()["rows"] == [[1]]

    def test_missing_auth_returns_401(self, auth_client):
        response = auth_client.post("/ingest", json=_metric_envelope())
        assert response.status_code == 401

    def test_invalid_key_returns_401(self, auth_client):
        response = auth_client.post(
            "/ingest",
            json=_metric_envelope(),
            headers={"Authorization": "Bearer wrong"},
        )
        assert response.status_code == 401

    def test_valid_bearer_passes(self, auth_client):
        response = auth_client.post(
            "/ingest",
            json=_metric_envelope(),
            headers={"Authorization": "Bearer secret123"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}


class TestArtifactEndpoints:
    def test_upload_then_download_round_trips(self, artifacts_client):
        env = {
            "project": "p",
            "study_name": "s",
            "trial_id": 0,
            "timestamp_ns": 1,
            "seq": 0,
            "artifact": {"key": "ckpt", "filename": "model.bin"},
        }
        artifacts_client.post("/ingest", json=env)

        resp = artifacts_client.post("/artifact/p/s/0/ckpt", content=b"model-bytes")
        assert resp.status_code == 200

        got = artifacts_client.get("/artifact/p/s/0/ckpt")
        assert got.status_code == 200
        assert got.content == b"model-bytes"
        assert "model.bin" in got.headers["content-disposition"]

    def test_download_missing_returns_404(self, artifacts_client):
        resp = artifacts_client.get("/artifact/p/s/0/nope")
        assert resp.status_code == 404

    def test_large_upload_round_trips(self, artifacts_client, tmp_path):
        body = bytes(range(256)) * 40960
        resp = artifacts_client.post("/artifact/p/s/0/big", content=body)
        assert resp.status_code == 200
        on_disk = (tmp_path / "artifacts" / "p" / "s" / "0" / "big").read_bytes()
        assert on_disk == body
        got = artifacts_client.get("/artifact/p/s/0/big")
        assert got.content == body
