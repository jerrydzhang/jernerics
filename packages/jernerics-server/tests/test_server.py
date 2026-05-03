import duckdb
import grpc
import httpx
import pytest
from jernerics_proto import ParamEvent, Value, tracking_pb2, tracking_pb2_grpc
from jernerics_server.server import serve


@pytest.fixture
def server_and_stub(tmp_path):
    port = 50052
    server = serve(tmp_path / "test.duckdb", port=port)
    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = tracking_pb2_grpc.TrackingServiceStub(channel)
    yield stub, tmp_path / "test.duckdb"
    channel.close()
    server.stop(grace=0)


class TestApiKeyInterceptor:
    def test_valid_key_passes(self, tmp_path):
        api_key = "test-secret"
        port = 50053
        server = serve(tmp_path / "test.duckdb", port=port, api_key=api_key)
        channel = grpc.insecure_channel(f"localhost:{port}")
        stub = tracking_pb2_grpc.TrackingServiceStub(channel)

        env = tracking_pb2.Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.01)),
        )
        ack = stub.SendEvent(env, metadata=[("x-api-key", api_key)])
        assert isinstance(ack, tracking_pb2.Ack)

        channel.close()
        server.stop(grace=0)

    def test_missing_key_rejected(self, tmp_path):
        api_key = "test-secret"
        port = 50053
        server = serve(tmp_path / "test.duckdb", port=port, api_key=api_key)
        channel = grpc.insecure_channel(f"localhost:{port}")
        stub = tracking_pb2_grpc.TrackingServiceStub(channel)

        env = tracking_pb2.Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.01)),
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.SendEvent(env)
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED  # ty: ignore[unresolved-attribute]

        channel.close()
        server.stop(grace=0)

    def test_invalid_key_rejected(self, tmp_path):
        api_key = "test-secret"
        port = 50053
        server = serve(tmp_path / "test.duckdb", port=port, api_key=api_key)
        channel = grpc.insecure_channel(f"localhost:{port}")
        stub = tracking_pb2_grpc.TrackingServiceStub(channel)

        env = tracking_pb2.Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.01)),
        )
        with pytest.raises(grpc.RpcError) as exc_info:
            stub.SendEvent(env, metadata=[("x-api-key", "wrong-key")])
        assert exc_info.value.code() == grpc.StatusCode.UNAUTHENTICATED  # ty: ignore[unresolved-attribute]

        channel.close()
        server.stop(grace=0)


class TestSendEvent:
    def test_param_event(self, server_and_stub):
        stub, db_path = server_and_stub
        env = tracking_pb2.Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.01)),
        )
        ack = stub.SendEvent(env)
        assert isinstance(ack, tracking_pb2.Ack)

        con = duckdb.connect(str(db_path))
        rows = con.execute("SELECT key, float_val FROM params").fetchall()
        con.close()
        assert rows == [("lr", 0.01)]

    def test_duplicate_seq_ignored(self, server_and_stub):
        stub, db_path = server_and_stub
        env = tracking_pb2.Envelope(
            project="p",
            study_name="s",
            trial_id=0,
            timestamp_ns=1000,
            seq=0,
            param=ParamEvent(key="lr", value=Value(float_val=0.01)),
        )
        stub.SendEvent(env)
        stub.SendEvent(env)

        con = duckdb.connect(str(db_path))
        rows = con.execute("SELECT COUNT(*) FROM params").fetchone()
        assert rows
        count = rows[0]
        con.close()
        assert count == 1


class TestGrpcHttpRoundTrip:
    def test_write_grpc_read_http(self, tmp_path):
        import time

        api_key = "test-secret"
        grpc_port = 50054
        http_port = 8084
        server = serve(
            tmp_path / "test.duckdb",
            port=grpc_port,
            http_port=http_port,
            http_host="127.0.0.1",
            api_key=api_key,
        )
        try:
            channel = grpc.insecure_channel(f"localhost:{grpc_port}")
            stub = tracking_pb2_grpc.TrackingServiceStub(channel)

            env = tracking_pb2.Envelope(
                project="p",
                study_name="s",
                trial_id=0,
                timestamp_ns=1000,
                seq=0,
                param=ParamEvent(key="lr", value=Value(float_val=0.01)),
            )
            stub.SendEvent(env, metadata=[("x-api-key", api_key)])
            channel.close()

            time.sleep(0.5)
            resp = httpx.post(
                f"http://127.0.0.1:{http_port}/query",
                json={"sql": "SELECT key, float_val FROM params"},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["columns"] == ["key", "float_val"]
            assert body["rows"] == [["lr", 0.01]]
        finally:
            server.stop(grace=0)
