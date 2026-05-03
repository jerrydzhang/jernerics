from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc
import uvicorn
from jernerics_proto import tracking_pb2, tracking_pb2_grpc

from .auth import ApiKeyInterceptor
from .http import create_app
from .store import DuckDBStore


class TrackingServicer(tracking_pb2_grpc.TrackingServiceServicer):
    def __init__(self, store: DuckDBStore) -> None:
        self._store = store

    def SendEvent(
        self, request: tracking_pb2.Envelope, context: grpc.ServicerContext
    ) -> tracking_pb2.Ack:
        _ = context  # Unused
        self._store.insert_event(request)
        return tracking_pb2.Ack()


def serve(
    db_path: str | Path,
    port: int = 50051,
    host: str = "[::]",
    api_key: str | None = None,
    http_port: int | None = None,
    http_host: str | None = None,
) -> grpc.Server:
    store = DuckDBStore(db_path)
    servicer = TrackingServicer(store)
    interceptors = [ApiKeyInterceptor(api_key)] if api_key else []
    server = grpc.server(ThreadPoolExecutor(max_workers=10), interceptors=interceptors)
    tracking_pb2_grpc.add_TrackingServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"{host}:{port}")
    server.start()

    if http_port is not None:
        app = create_app(store, api_key=api_key)
        bind_host = http_host or host
        config = uvicorn.Config(app, host=bind_host, port=http_port, log_level="error")
        uvicorn_server = uvicorn.Server(config)
        import threading

        thread = threading.Thread(target=uvicorn_server.run, daemon=True)
        thread.start()

    return server
