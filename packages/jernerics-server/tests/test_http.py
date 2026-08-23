import sqlite3

import pytest
from fastapi.testclient import TestClient
from jernerics_server.http import create_app
from jernerics_server.store import Store


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    app = create_app(store)
    return TestClient(app)


@pytest.fixture
def auth_client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    app = create_app(store, api_key="secret123")
    return TestClient(app)


def _seed_trial(path, number: int = 7, state: str = "failed") -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(
        "INSERT INTO sweeps (sweep_id, project, name, state, created_ns,"
        " updated_ns) VALUES ('sw', 'p', 'n', 'running', 1, 1)"
    )
    con.execute(
        "INSERT INTO trials (trial_id, sweep_id, number, state,"
        " retry_root_trial_id, retry_index, created_ns, updated_ns)"
        f" VALUES ('t1', 'sw', {number}, '{state}', 't1', 0, 1, 1)"
    )
    con.commit()
    con.close()


class TestQueryEndpoint:
    def test_valid_select_returns_columns_and_rows(self, client):
        response = client.post("/query", json={"sql": "SELECT 1 AS n, 'hello' AS s"})
        assert response.status_code == 200
        body = response.json()
        assert body["columns"] == ["n", "s"]
        assert body["rows"] == [[1, "hello"]]

    def test_rejects_insert(self, client):
        response = client.post(
            "/query", json={"sql": "INSERT INTO trials VALUES ('t', 1)"}
        )
        assert response.status_code == 400
        body = response.json()
        assert "error" in body

    def test_rejects_delete(self, client):
        response = client.post("/query", json={"sql": "DELETE FROM trials"})
        assert response.status_code == 400

    def test_rejects_drop(self, client):
        response = client.post("/query", json={"sql": "DROP TABLE trials"})
        assert response.status_code == 400

    def test_rejects_update(self, client):
        response = client.post(
            "/query", json={"sql": "UPDATE trials SET state = 'failed'"}
        )
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

    def test_query_binds_params(self, client, tmp_path):
        _seed_trial(tmp_path / "test.sqlite")
        response = client.post(
            "/query",
            json={
                "sql": "SELECT number FROM trials WHERE state = ?",
                "params": ["failed"],
            },
        )
        assert response.status_code == 200
        assert response.json()["rows"] == [[7]]

    def test_query_without_params_still_works(self, client):
        response = client.post("/query", json={"sql": "SELECT 1 AS n"})
        assert response.status_code == 200
        assert response.json()["rows"] == [[1]]

    def test_valid_cte_select_returns_rows(self, client):
        response = client.post(
            "/query", json={"sql": "WITH c AS (SELECT 1 AS n) SELECT n FROM c"}
        )
        assert response.status_code == 200
        assert response.json()["rows"] == [[1]]

    def test_rejects_cte_delete(self, client):
        response = client.post(
            "/query", json={"sql": "WITH c AS (SELECT 1) DELETE FROM trials"}
        )
        assert response.status_code == 400
        assert response.json() == {"error": "Only SELECT queries are allowed"}

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH c AS (SELECT 1) INSERT INTO trials (trial_id) VALUES ('x')",
            "WITH c AS (SELECT 1) UPDATE trials SET state = 'failed'",
        ],
    )
    def test_rejects_cte_insert_and_update(self, client, sql):
        response = client.post("/query", json={"sql": sql})
        assert response.status_code == 400
        assert response.json() == {"error": "Only SELECT queries are allowed"}


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
