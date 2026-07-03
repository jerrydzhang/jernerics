import sys
import time
from pathlib import Path
from queue import Queue
from threading import Thread

import httpx

from .jsonl_io import TrackingReader


class StreamClient:
    """Tails the local JSONL buffer and ships each envelope to /ingest live.

    Live observability is first-class: metrics appear on the server as the
    trial runs. The local file remains the durable source of truth; a later
    replay (batch_sync) re-sends anything this client failed to confirm, and
    the server's INSERT OR IGNORE makes the overlap safe.
    """

    def __init__(
        self,
        base_url: str,
        path: Path,
        api_key: str | None = None,
        poll_interval: float = 0.5,
        flush_timeout: float = 60.0,
        send_deadline: float = 30.0,
        max_retry_time: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.flush_timeout = flush_timeout
        self.send_deadline = send_deadline
        self.max_retry_time = max_retry_time
        self._headers = {"authorization": f"Bearer {api_key}"} if api_key else None
        self.producer_thread = Thread(target=self._read_file_buffer, daemon=True)
        self.consumer = Thread(target=self._consume_buffer, daemon=True)
        self.buffer: Queue[dict] = Queue(maxsize=10000)

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

                if "trial_end" in event:
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
                    response = httpx.post(
                        f"{self.base_url}/ingest",
                        json=event,
                        headers=self._headers,
                        timeout=self.send_deadline,
                    )
                    response.raise_for_status()
                    retry_count = 0
                    retry_start = time.monotonic()
                    sent = True
                except httpx.HTTPError:
                    print(
                        f"Failed to send event, retry {retry_count + 1} ...",
                        file=sys.stderr,
                    )
                    wait_time: float = min(self.poll_interval * 2**retry_count, 10)
                    retry_count += 1
                    time.sleep(wait_time)

            if "trial_end" in event:
                break
