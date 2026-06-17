from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from jernerics_proto import (
    ArtifactEvent,
    Envelope,
    MetricEvent,
    ParamEvent,
    ResultEvent,
    SweepMetaEvent,
    TrialEndEvent,
    Value,
)
from jernerics_server.http import create_app
from jernerics_server.store import Store


def _fake_s3_fetch(bucket: str, key: str) -> tuple[BytesIO, str]:
    return BytesIO(b"fake-model-data"), key


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
    app = create_app(store, s3_fetch=_fake_s3_fetch)
    app.state.store = store
    return TestClient(app)


@pytest.fixture
def auth_client(tmp_path):
    store = Store(tmp_path / "test.sqlite")
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

    def test_404_for_missing_key(self, client):
        response = client.get("/artifact/nope/nope/0/nope")
        assert response.status_code == 404

    def test_404_for_missing_file_in_s3(self, tmp_path):
        def raise_not_found(bucket, key):
            raise FileNotFoundError("not found")

        store = Store(tmp_path / "test.sqlite")
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

        store = Store(tmp_path / "test.sqlite")
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


class TestSweepsEndpoint:
    def test_empty_sweeps_returns_empty_list(self, client):
        response = client.get("/api/sweeps")
        assert response.status_code == 200
        assert response.json() == []

    def test_one_sweep_with_multiple_trials(self, client):
        db = client.app.state.store

        # Insert sweep meta
        env_sweep_meta = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            sweep_meta=SweepMetaEvent(git_hash="abc123", config="{}"),
        )
        db.insert_event(env_sweep_meta)

        # Insert params for trial 0
        env_param0 = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=2000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.001)),
        )
        db.insert_event(env_param0)

        # Insert params for trial 1
        env_param1 = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=1,
            timestamp_ns=3000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.01)),
        )
        db.insert_event(env_param1)

        # Insert metrics for trial 0
        env_metric0 = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=4000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=1),
        )
        db.insert_event(env_metric0)

        # Insert trial end for trial 0
        env_trial_end0 = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=5000,
            seq=0,
            trial_end=TrialEndEvent(),
        )
        db.insert_event(env_trial_end0)

        response = client.get("/api/sweeps")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["project"] == "myproj"
        assert body[0]["study_name"] == "mystudy"
        assert body[0]["trial_count"] == 2
        assert body[0]["completed_count"] == 1
        assert body[0]["last_event_timestamp_ns"] == 5000

    def test_multiple_sweeps(self, client):
        db = client.app.state.store

        # Sweep 1
        env1 = Envelope(
            project="proj1",
            study_name="study1",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env1)

        # Sweep 2
        env2 = Envelope(
            project="proj2",
            study_name="study2",
            trial_id=0,
            timestamp_ns=2000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.1),
        )
        db.insert_event(env2)

        response = client.get("/api/sweeps")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2

        # Sort by project and study_name for predictable order
        sorted_body = sorted(body, key=lambda x: (x["project"], x["study_name"]))
        assert sorted_body[0]["project"] == "proj1"
        assert sorted_body[0]["study_name"] == "study1"
        assert sorted_body[0]["trial_count"] == 1
        assert sorted_body[0]["completed_count"] == 0
        assert sorted_body[0]["last_event_timestamp_ns"] == 1000

        assert sorted_body[1]["project"] == "proj2"
        assert sorted_body[1]["study_name"] == "study2"
        assert sorted_body[1]["trial_count"] == 1
        assert sorted_body[1]["completed_count"] == 0
        assert sorted_body[1]["last_event_timestamp_ns"] == 2000

    def test_requires_bearer_auth(self, auth_client):
        response = auth_client.get("/api/sweeps")
        assert response.status_code == 401

        response = auth_client.get(
            "/api/sweeps", headers={"Authorization": "Bearer secret123"}
        )
        assert response.status_code == 200

    def test_trial_count_aggregates_across_all_tables(self, client):
        db = client.app.state.store

        # Add params for trial 0
        env_param = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.001)),
        )
        db.insert_event(env_param)

        # Add metrics for trial 1
        env_metric = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=2000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5),
        )
        db.insert_event(env_metric)

        # Add results for trial 2
        env_result = Envelope(
            project="p",
            study_name="s",
            trial_id=2,
            timestamp_ns=3000,
            seq=0,
            result=ResultEvent(key="result", value="{}"),
        )
        db.insert_event(env_result)

        # Add artifacts for trial 3
        env_artifact = Envelope(
            project="p",
            study_name="s",
            trial_id=3,
            timestamp_ns=4000,
            seq=0,
            artifact=ArtifactEvent(key="model", filename="model.bin"),
        )
        db.insert_event(env_artifact)

        # Add sweep_meta for trial 4
        env_sweep_meta = Envelope(
            project="p",
            study_name="s",
            trial_id=4,
            timestamp_ns=5000,
            seq=0,
            sweep_meta=SweepMetaEvent(git_hash="abc", config="{}"),
        )
        db.insert_event(env_sweep_meta)

        # Add trial_end for trial 5
        env_trial_end = Envelope(
            project="p",
            study_name="s",
            trial_id=5,
            timestamp_ns=6000,
            seq=0,
            trial_end=TrialEndEvent(),
        )
        db.insert_event(env_trial_end)

        response = client.get("/api/sweeps")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["trial_count"] == 6

    def test_completed_count_counts_only_trial_end(self, client):
        db = client.app.state.store

        # Trial 0: has trial_end
        env_end0 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            trial_end=TrialEndEvent(),
        )
        db.insert_event(env_end0)

        # Trial 1: has trial_end
        env_end1 = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=2000,
            seq=0,
            trial_end=TrialEndEvent(),
        )
        db.insert_event(env_end1)

        # Trial 2: only has param, not ended
        env_param2 = Envelope(
            project="p",
            study_name="s",
            trial_id=2,
            timestamp_ns=3000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env_param2)

        response = client.get("/api/sweeps")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["trial_count"] == 3
        assert body[0]["completed_count"] == 2

    def test_last_event_timestamp_ns_is_max_across_all_tables(self, client):
        db = client.app.state.store

        # Add events with different timestamps
        env1 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env1)

        env2 = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=5000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.1),
        )
        db.insert_event(env2)

        env3 = Envelope(
            project="p",
            study_name="s",
            trial_id=2,
            timestamp_ns=3000,
            seq=0,
            result=ResultEvent(key="r", value="{}"),
        )
        db.insert_event(env3)

        env4 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=7000,
            seq=1,
            trial_end=TrialEndEvent(),
        )
        db.insert_event(env4)

        response = client.get("/api/sweeps")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["last_event_timestamp_ns"] == 7000
