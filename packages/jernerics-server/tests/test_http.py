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
