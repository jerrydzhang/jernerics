import sys
import time
from pathlib import Path
from queue import Queue
from threading import Thread

import grpc
from jernerics_proto import tracking_pb2_grpc
from jernerics_proto.tracking_pb2 import Envelope

from .wire import TrackingReader


class StreamClient:
    def __init__(
        self,
        stub: tracking_pb2_grpc.TrackingServiceStub,
        path: Path,
        poll_interval: float = 0.5,
        flush_timeout: float = 60.0,
    ):
        self.stub: tracking_pb2_grpc.TrackingServiceStub = stub
        self.path: Path = path
        self.poll_interval: float = poll_interval
        self.flush_timeout: float = flush_timeout
        self.producer_thread = Thread(target=self._read_file_buffer, daemon=True)
        self.consumer = Thread(target=self._consume_buffer, daemon=True)
        self.buffer: Queue[Envelope] = Queue(maxsize=10000)

    def start(self) -> None:
        self.producer_thread.start()
        self.consumer.start()

    def join(self) -> None:
        self.producer_thread.join(timeout=self.flush_timeout)
        self.consumer.join(timeout=self.flush_timeout)

        if self.producer_thread.is_alive() or self.consumer.is_alive():
            print(
                "Warning: Sync threads did not finish within flush timeout. "
                "There may be unsent events.",
                file=sys.stderr,
            )

    def _read_file_buffer(self) -> None:
        while not self.path.exists():
            time.sleep(self.poll_interval)

        reader = TrackingReader(self.path)
        with reader:
            while True:
                event = reader.try_read_envelope()

                if not event:
                    time.sleep(self.poll_interval)
                    continue

                self.buffer.put(event)

                if event.WhichOneof("payload") == "trial_end":
                    break

    def _consume_buffer(self) -> None:
        retry_count = 0
        while True:
            event = self.buffer.get()

            sent = False
            while not sent:
                try:
                    self.stub.SendEvent(event)
                    retry_count = 0
                    sent = True
                except grpc.RpcError:
                    print(
                        f"Failed to send event, retry {retry_count + 1} ...",
                        file=sys.stderr,
                    )
                    wait_time: float = min(self.poll_interval * 2**retry_count, 10)
                    retry_count += 1
                    time.sleep(wait_time)

            if event.WhichOneof("payload") == "trial_end":
                break
