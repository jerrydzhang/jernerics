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

    def test_filters_by_project(self, client):
        db = client.app.state.store

        # Sweep for proj1
        env1 = Envelope(
            project="proj1",
            study_name="study1",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env1)

        # Sweep for proj2
        env2 = Envelope(
            project="proj2",
            study_name="study2",
            trial_id=0,
            timestamp_ns=2000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.1),
        )
        db.insert_event(env2)

        # Query without project filter
        response = client.get("/api/sweeps")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2

        # Query with project filter for proj1
        response = client.get("/api/sweeps?project=proj1")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["project"] == "proj1"
        assert body[0]["study_name"] == "study1"

        # Query with project filter for proj2
        response = client.get("/api/sweeps?project=proj2")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["project"] == "proj2"
        assert body[0]["study_name"] == "study2"

        # Query with non-existent project
        response = client.get("/api/sweeps?project=nonexistent")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 0

    def test_url_encodes_project_parameter(self, client):
        db = client.app.state.store

        # Sweep with spaces in project name
        env = Envelope(
            project="my project",
            study_name="study1",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env)

        response = client.get("/api/sweeps?project=my%20project")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["project"] == "my project"


class TestTrialsEndpoint:
    def test_valid_request(self, client):
        db = client.app.state.store

        # Insert params for trial 0
        env_param0 = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.001)),
        )
        db.insert_event(env_param0)

        # Insert final metric for trial 0
        env_metric0 = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=2000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=-1),
        )
        db.insert_event(env_metric0)

        # Insert artifact for trial 0
        env_artifact0 = Envelope(
            project="myproj",
            study_name="mystudy",
            trial_id=0,
            timestamp_ns=3000,
            seq=0,
            artifact=ArtifactEvent(key="model", filename="model.bin"),
        )
        db.insert_event(env_artifact0)

        response = client.get("/api/trials?project=myproj&study_name=mystudy")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["trial_id"] == 0
        assert body[0]["status"] == "incomplete"
        assert body[0]["params"] == {"lr": 0.001}
        assert body[0]["final_metrics"] == {"loss": 0.5}
        assert body[0]["artifact_keys"] == ["model"]

    def test_missing_auth(self, auth_client):
        response = auth_client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 401

        response = auth_client.get(
            "/api/trials?project=p&study_name=s",
            headers={"Authorization": "Bearer secret123"},
        )
        assert response.status_code == 200

    def test_missing_params(self, client):
        response = client.get("/api/trials")
        assert response.status_code == 422  # FastAPI validation error

    def test_empty_results(self, client):
        response = client.get("/api/trials?project=nonexistent&study_name=nonexistent")
        assert response.status_code == 200
        assert response.json() == []

    def test_multiple_trials_sorted(self, client):
        db = client.app.state.store

        # Trial 2 first (out of order)
        env_param2 = Envelope(
            project="p",
            study_name="s",
            trial_id=2,
            timestamp_ns=3000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=2)),
        )
        db.insert_event(env_param2)

        # Trial 0
        env_param0 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=0)),
        )
        db.insert_event(env_param0)

        env_end0 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=0,
            trial_end=TrialEndEvent(),
        )
        db.insert_event(env_end0)

        # Trial 1
        env_param1 = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=4000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env_param1)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 3
        assert body[0]["trial_id"] == 0
        assert body[1]["trial_id"] == 1
        assert body[2]["trial_id"] == 2
        assert body[0]["status"] == "complete"
        assert body[1]["status"] == "incomplete"
        assert body[2]["status"] == "incomplete"

    def test_status_complete_when_trial_end_exists(self, client):
        db = client.app.state.store

        env_param = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env_param)

        env_end = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=0,
            trial_end=TrialEndEvent(),
        )
        db.insert_event(env_end)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["status"] == "complete"

    def test_status_incomplete_when_no_trial_end(self, client):
        db = client.app.state.store

        env_param = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env_param)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["status"] == "incomplete"

    def test_params_with_different_types(self, client):
        db = client.app.state.store

        env_param_float = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.001)),
        )
        db.insert_event(env_param_float)

        env_param_int = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=1,
            param=ParamEvent(key="batch_size", value=Value(int_val=32)),
        )
        db.insert_event(env_param_int)

        env_param_string = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=3000,
            seq=2,
            param=ParamEvent(key="optimizer", value=Value(string_val="adam")),
        )
        db.insert_event(env_param_string)

        env_param_bool = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=4000,
            seq=3,
            param=ParamEvent(key="use_bn", value=Value(bool_val=True)),
        )
        db.insert_event(env_param_bool)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["params"] == {
            "lr": 0.001,
            "batch_size": 32,
            "optimizer": "adam",
            "use_bn": True,
        }

    def test_final_metrics_excludes_step_metrics(self, client):
        db = client.app.state.store

        # Final metric (step IS NULL, represented as step=-1 in proto)
        env_metric_final = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=-1),
        )
        db.insert_event(env_metric_final)

        # Intermediate metric (step IS NOT NULL)
        env_metric_intermediate = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=1,
            metric=MetricEvent(key="loss", value=0.8, step=1),
        )
        db.insert_event(env_metric_intermediate)

        env_metric_acc = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=3000,
            seq=2,
            metric=MetricEvent(key="accuracy", value=0.9, step=-1),
        )
        db.insert_event(env_metric_acc)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["final_metrics"] == {"loss": 0.5, "accuracy": 0.9}

    def test_artifact_keys_lists_all_artifacts(self, client):
        db = client.app.state.store

        env_artifact1 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            artifact=ArtifactEvent(key="model", filename="model.bin"),
        )
        db.insert_event(env_artifact1)

        env_artifact2 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=1,
            artifact=ArtifactEvent(key="plot", filename="loss.png"),
        )
        db.insert_event(env_artifact2)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert sorted(body[0]["artifact_keys"]) == ["model", "plot"]

    def test_empty_params_metrics_artifacts(self, client):
        db = client.app.state.store

        # Trial with only results (no params, metrics, or artifacts)
        env_result = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            result=ResultEvent(key="result", value="{}"),
        )
        db.insert_event(env_result)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["params"] == {}
        assert body[0]["final_metrics"] == {}
        assert body[0]["artifact_keys"] == []

    def test_trials_from_multiple_tables(self, client):
        db = client.app.state.store

        # Trial 0: has params
        env_param0 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="x", value=Value(int_val=1)),
        )
        db.insert_event(env_param0)

        # Trial 1: has metrics
        env_metric1 = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=2000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5),
        )
        db.insert_event(env_metric1)

        # Trial 2: has results
        env_result2 = Envelope(
            project="p",
            study_name="s",
            trial_id=2,
            timestamp_ns=3000,
            seq=0,
            result=ResultEvent(key="result", value="{}"),
        )
        db.insert_event(env_result2)

        # Trial 3: has artifacts
        env_artifact3 = Envelope(
            project="p",
            study_name="s",
            trial_id=3,
            timestamp_ns=4000,
            seq=0,
            artifact=ArtifactEvent(key="model", filename="model.bin"),
        )
        db.insert_event(env_artifact3)

        response = client.get("/api/trials?project=p&study_name=s")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 4
        trial_ids = [t["trial_id"] for t in body]
        assert trial_ids == [0, 1, 2, 3]

    def test_metric_keys_filter_single_key(self, client):
        db = client.app.state.store

        env_metric1 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=-1),
        )
        db.insert_event(env_metric1)

        env_metric2 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=1,
            metric=MetricEvent(key="accuracy", value=0.95, step=-1),
        )
        db.insert_event(env_metric2)

        response = client.get("/api/trials?project=p&study_name=s&metric_keys=loss")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["final_metrics"] == {"loss": 0.5}

    def test_metric_keys_filter_multiple_keys(self, client):
        db = client.app.state.store

        env_metric1 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=-1),
        )
        db.insert_event(env_metric1)

        env_metric2 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=1,
            metric=MetricEvent(key="accuracy", value=0.95, step=-1),
        )
        db.insert_event(env_metric2)

        env_metric3 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=3000,
            seq=2,
            metric=MetricEvent(key="f1", value=0.87, step=-1),
        )
        db.insert_event(env_metric3)

        response = client.get(
            "/api/trials?project=p&study_name=s&metric_keys=loss,accuracy"
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["final_metrics"] == {"loss": 0.5, "accuracy": 0.95}
        assert "f1" not in body[0]["final_metrics"]

    def test_metric_keys_filter_invalid_keys_omitted(self, client):
        db = client.app.state.store

        env_metric = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=-1),
        )
        db.insert_event(env_metric)

        response = client.get(
            "/api/trials?project=p&study_name=s&metric_keys=loss,nonexistent"
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["final_metrics"] == {"loss": 0.5}

    def test_metric_keys_all_invalid_returns_empty_metrics(self, client):
        db = client.app.state.store

        env_metric = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=-1),
        )
        db.insert_event(env_metric)

        response = client.get(
            "/api/trials?project=p&study_name=s&metric_keys=invalid1,invalid2"
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["final_metrics"] == {}

    def test_metric_keys_multiple_trials(self, client):
        db = client.app.state.store

        # Trial 0
        env_metric0_1 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.5, step=-1),
        )
        db.insert_event(env_metric0_1)

        env_metric0_2 = Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=2000,
            seq=1,
            metric=MetricEvent(key="accuracy", value=0.95, step=-1),
        )
        db.insert_event(env_metric0_2)

        # Trial 1
        env_metric1_1 = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=3000,
            seq=0,
            metric=MetricEvent(key="loss", value=0.3, step=-1),
        )
        db.insert_event(env_metric1_1)

        env_metric1_2 = Envelope(
            project="p",
            study_name="s",
            trial_id=1,
            timestamp_ns=4000,
            seq=1,
            metric=MetricEvent(key="accuracy", value=0.97, step=-1),
        )
        db.insert_event(env_metric1_2)

        response = client.get("/api/trials?project=p&study_name=s&metric_keys=loss")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        assert body[0]["final_metrics"] == {"loss": 0.5}
        assert body[1]["final_metrics"] == {"loss": 0.3}


class TestCompareSweepsEndpoint:
    def test_valid_request_returns_comparison(self, client):
        db = client.app.state.store

        # Left sweep: studyA with params lr, batch_size,
        # metrics loss, accuracy, artifacts model
        for trial_id in range(3):
            env_param_lr = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=1000 + trial_id,
                seq=0,
                param=ParamEvent(
                    key="lr", value=Value(float_val=0.001 * (trial_id + 1))
                ),
            )
            db.insert_event(env_param_lr)

            env_param_bs = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=2000 + trial_id,
                seq=1,
                param=ParamEvent(key="batch_size", value=Value(int_val=32)),
            )
            db.insert_event(env_param_bs)

            env_metric_loss = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=3000 + trial_id,
                seq=0,
                metric=MetricEvent(key="loss", value=0.5 - trial_id * 0.1, step=-1),
            )
            db.insert_event(env_metric_loss)

            env_metric_acc = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=4000 + trial_id,
                seq=1,
                metric=MetricEvent(
                    key="accuracy", value=0.8 + trial_id * 0.05, step=-1
                ),
            )
            db.insert_event(env_metric_acc)

            env_artifact = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=5000 + trial_id,
                seq=0,
                artifact=ArtifactEvent(key="model", filename="model.bin"),
            )
            db.insert_event(env_artifact)

            env_end = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=6000 + trial_id,
                seq=0,
                trial_end=TrialEndEvent(),
            )
            db.insert_event(env_end)

        # Right sweep: studyB with params lr, dropout,
        # metrics loss, f1, artifacts model, plot
        for trial_id in range(2):
            env_param_lr = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=7000 + trial_id,
                seq=0,
                param=ParamEvent(
                    key="lr", value=Value(float_val=0.01 * (trial_id + 1))
                ),
            )
            db.insert_event(env_param_lr)

            env_param_do = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=8000 + trial_id,
                seq=1,
                param=ParamEvent(key="dropout", value=Value(float_val=0.2)),
            )
            db.insert_event(env_param_do)

            env_metric_loss = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=9000 + trial_id,
                seq=0,
                metric=MetricEvent(key="loss", value=0.6 - trial_id * 0.1, step=-1),
            )
            db.insert_event(env_metric_loss)

            env_metric_f1 = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=10000 + trial_id,
                seq=1,
                metric=MetricEvent(key="f1", value=0.7 + trial_id * 0.1, step=-1),
            )
            db.insert_event(env_metric_f1)

            env_artifact_model = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=11000 + trial_id,
                seq=0,
                artifact=ArtifactEvent(key="model", filename="model.bin"),
            )
            db.insert_event(env_artifact_model)

            env_artifact_plot = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=12000 + trial_id,
                seq=1,
                artifact=ArtifactEvent(key="plot", filename="loss.png"),
            )
            db.insert_event(env_artifact_plot)

            env_end = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=13000 + trial_id,
                seq=0,
                trial_end=TrialEndEvent(),
            )
            db.insert_event(env_end)

        response = client.get(
            "/api/compare-sweeps?project=proj&left=studyA&right=studyB"
        )
        assert response.status_code == 200
        body = response.json()

        assert body["left"] == "studyA"
        assert body["right"] == "studyB"
        assert body["left_trial_count"] == 3
        assert body["left_completed_count"] == 3
        assert body["right_trial_count"] == 2
        assert body["right_completed_count"] == 2

        # Params: shared {lr}, left_only {batch_size}, right_only {dropout}
        assert set(body["param_keys"]["shared"]) == {"lr"}
        assert set(body["param_keys"]["left_only"]) == {"batch_size"}
        assert set(body["param_keys"]["right_only"]) == {"dropout"}

        # Final metrics: shared {loss}, left_only {accuracy}, right_only {f1}
        assert set(body["final_metric_keys"]["shared"]) == {"loss"}
        assert set(body["final_metric_keys"]["left_only"]) == {"accuracy"}
        assert set(body["final_metric_keys"]["right_only"]) == {"f1"}

        # Artifacts: shared {model}, left_only {}, right_only {plot}
        assert set(body["artifact_keys"]["shared"]) == {"model"}
        assert set(body["artifact_keys"]["left_only"]) == set()
        assert set(body["artifact_keys"]["right_only"]) == {"plot"}

        # Metric stats for shared loss metric
        assert "loss" in body["final_metric_stats"]
        assert (
            body["final_metric_stats"]["loss"]["left"]["min"] == 0.3  # noqa: RUF069
        )
        assert (
            body["final_metric_stats"]["loss"]["left"]["median"] == 0.4  # noqa: RUF069
        )
        assert (
            body["final_metric_stats"]["loss"]["left"]["max"] == 0.5  # noqa: RUF069
        )
        assert (
            body["final_metric_stats"]["loss"]["right"]["min"] == 0.5  # noqa: RUF069
        )
        assert (
            body["final_metric_stats"]["loss"]["right"]["median"]  # noqa: RUF069
            == 0.55
        )
        assert (
            body["final_metric_stats"]["loss"]["right"]["max"] == 0.6  # noqa: RUF069
        )

    def test_missing_query_params_returns_422(self, client):
        response = client.get("/api/compare-sweeps")
        assert response.status_code == 422

        response = client.get("/api/compare-sweeps?project=proj")
        assert response.status_code == 422

        response = client.get("/api/compare-sweeps?project=proj&left=studyA")
        assert response.status_code == 422

    def test_nonexistent_study_returns_404(self, client):
        response = client.get(
            "/api/compare-sweeps?project=proj&left=nonexistent&right=also_nonexistent"
        )
        assert response.status_code == 404

    def test_requires_bearer_auth(self, auth_client):
        db = auth_client.app.state.store

        # Create two empty sweeps
        for study_name in ["studyA", "studyB"]:
            env = Envelope(
                project="proj",
                study_name=study_name,
                trial_id=0,
                timestamp_ns=1000,
                seq=0,
                sweep_meta=SweepMetaEvent(git_hash="abc", config="{}"),
            )
            db.insert_event(env)

        response = auth_client.get(
            "/api/compare-sweeps?project=proj&left=studyA&right=studyB"
        )
        assert response.status_code == 401

        response = auth_client.get(
            "/api/compare-sweeps?project=proj&left=studyA&right=studyB",
            headers={"Authorization": "Bearer secret123"},
        )
        assert response.status_code == 200

    def test_no_auth_when_key_not_set(self, client):
        db = client.app.state.store

        # Create two empty sweeps
        for study_name in ["studyA", "studyB"]:
            env = Envelope(
                project="proj",
                study_name=study_name,
                trial_id=0,
                timestamp_ns=1000,
                seq=0,
                sweep_meta=SweepMetaEvent(git_hash="abc", config="{}"),
            )
            db.insert_event(env)

        response = client.get(
            "/api/compare-sweeps?project=proj&left=studyA&right=studyB"
        )
        assert response.status_code == 200

    def test_empty_sweeps_comparison(self, client):
        db = client.app.state.store

        # Create two empty sweeps (only sweep meta)
        for study_name in ["studyA", "studyB"]:
            env = Envelope(
                project="proj",
                study_name=study_name,
                trial_id=0,
                timestamp_ns=1000,
                seq=0,
                sweep_meta=SweepMetaEvent(git_hash="abc", config="{}"),
            )
            db.insert_event(env)

        response = client.get(
            "/api/compare-sweeps?project=proj&left=studyA&right=studyB"
        )
        assert response.status_code == 200
        body = response.json()

        assert body["left_trial_count"] == 1
        assert body["left_completed_count"] == 0
        assert body["right_trial_count"] == 1
        assert body["right_completed_count"] == 0
        assert body["param_keys"]["shared"] == []
        assert body["param_keys"]["left_only"] == []
        assert body["param_keys"]["right_only"] == []
        assert body["final_metric_keys"]["shared"] == []
        assert body["final_metric_keys"]["left_only"] == []
        assert body["final_metric_keys"]["right_only"] == []
        assert body["artifact_keys"]["shared"] == []
        assert body["artifact_keys"]["left_only"] == []
        assert body["artifact_keys"]["right_only"] == []
        assert body["final_metric_stats"] == {}

    def test_incomplete_trials_counted_but_not_in_metrics(self, client):
        db = client.app.state.store

        # Left: 3 trials, 2 completed
        for trial_id in range(3):
            env_param = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=1000 + trial_id,
                seq=0,
                param=ParamEvent(key="lr", value=Value(float_val=0.001)),
            )
            db.insert_event(env_param)

            env_metric = Envelope(
                project="proj",
                study_name="studyA",
                trial_id=trial_id,
                timestamp_ns=2000 + trial_id,
                seq=0,
                metric=MetricEvent(key="loss", value=0.5 - trial_id * 0.1, step=-1),
            )
            db.insert_event(env_metric)

            if trial_id < 2:
                env_end = Envelope(
                    project="proj",
                    study_name="studyA",
                    trial_id=trial_id,
                    timestamp_ns=3000 + trial_id,
                    seq=0,
                    trial_end=TrialEndEvent(),
                )
                db.insert_event(env_end)

        # Right: 2 trials, both completed
        for trial_id in range(2):
            env_param = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=4000 + trial_id,
                seq=0,
                param=ParamEvent(key="lr", value=Value(float_val=0.01)),
            )
            db.insert_event(env_param)

            env_metric = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=5000 + trial_id,
                seq=0,
                metric=MetricEvent(key="loss", value=0.6 - trial_id * 0.1, step=-1),
            )
            db.insert_event(env_metric)

            env_end = Envelope(
                project="proj",
                study_name="studyB",
                trial_id=trial_id,
                timestamp_ns=6000 + trial_id,
                seq=0,
                trial_end=TrialEndEvent(),
            )
            db.insert_event(env_end)

        response = client.get(
            "/api/compare-sweeps?project=proj&left=studyA&right=studyB"
        )
        assert response.status_code == 200
        body = response.json()

        assert body["left_trial_count"] == 3
        assert body["left_completed_count"] == 2
        assert body["right_trial_count"] == 2
        assert body["right_completed_count"] == 2

        # Metric stats should only include completed trials
        # studyA: trials 0 and 1 completed with loss 0.5 and 0.4
        # studyB: trials 0 and 1 completed with loss 0.6 and 0.5
        assert (
            body["final_metric_stats"]["loss"]["left"]["min"] == 0.4  # noqa: RUF069
        )
        assert (
            body["final_metric_stats"]["loss"]["left"]["median"]  # noqa: RUF069
            == 0.45
        )
        assert (
            body["final_metric_stats"]["loss"]["left"]["max"] == 0.5  # noqa: RUF069
        )
        assert (
            body["final_metric_stats"]["loss"]["right"]["min"] == 0.5  # noqa: RUF069
        )
        assert (
            body["final_metric_stats"]["loss"]["right"]["median"]  # noqa: RUF069
            == 0.55
        )
        assert (
            body["final_metric_stats"]["loss"]["right"]["max"] == 0.6  # noqa: RUF069
        )


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
