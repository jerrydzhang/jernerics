import grpc
import pytest
from jernerics_proto import tracking_pb2_grpc
from jernerics_server.server import serve


@pytest.fixture
def grpc_server(tmp_path):
    """Function-scoped gRPC server on a random port with a fresh DuckDB."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    db_path = tmp_path / "tracking.duckdb"
    server = serve(db_path, port=port, host="127.0.0.1")
    channel = grpc.insecure_channel(f"localhost:{port}")
    stub = tracking_pb2_grpc.TrackingServiceStub(channel)
    yield stub, db_path, port
    channel.close()
    server.stop(grace=0)
