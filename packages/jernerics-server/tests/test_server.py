from __future__ import annotations

import duckdb
import grpc
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
