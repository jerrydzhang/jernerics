from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc
from jernerics_proto import tracking_pb2, tracking_pb2_grpc

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


def serve(db_path: str | Path, port: int = 50051, host: str = "[::]") -> grpc.Server:
    store = DuckDBStore(db_path)
    servicer = TrackingServicer(store)
    server = grpc.server(ThreadPoolExecutor(max_workers=10))
    tracking_pb2_grpc.add_TrackingServiceServicer_to_server(servicer, server)
    server.add_insecure_port(f"{host}:{port}")
    server.start()
    return server
