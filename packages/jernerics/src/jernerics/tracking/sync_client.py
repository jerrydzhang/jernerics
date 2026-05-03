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
        send_deadline: float = 30.0,
        max_retry_time: float = 300.0,
        api_key: str | None = None,
    ):
        self.stub: tracking_pb2_grpc.TrackingServiceStub = stub
        self.path: Path = path
        self.poll_interval: float = poll_interval
        self.flush_timeout: float = flush_timeout
        self.send_deadline: float = send_deadline
        self.max_retry_time: float = max_retry_time
        self._metadata: list[tuple[str, str]] | None = (
            [("x-api-key", api_key)] if api_key else None
        )
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
        retry_start = time.monotonic()
        while True:
            event = self.buffer.get()

            sent = False
            while not sent:
                elapsed = time.monotonic() - retry_start
                if elapsed > self.max_retry_time:
                    print(
                        f"Warning: exceeded max retry time ({self.max_retry_time}s)."
                        " Dropping remaining events.",
                        file=sys.stderr,
                    )
                    return

                try:
                    self.stub.SendEvent(
                        event,
                        timeout=self.send_deadline,
                        metadata=self._metadata,
                    )
                    retry_count = 0
                    retry_start = time.monotonic()
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
