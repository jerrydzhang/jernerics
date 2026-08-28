"""Live batched shipper for the local JSONL event log.

Tails the trial's event file from the durable byte cursor and ships batches
to ``POST /ingest``: up to ``batch_size`` events, or every ``batch_window``
seconds after the first pending event, whichever comes first. The cursor
advances only after a 2xx acknowledgement, so a crashed or timed-out shipper
leaves unacked events to be re-read with unchanged event ids (the ids live in
the JSONL bytes). A failed batch is retried with exponential backoff; the
request body stays byte-identical for permanent failures (server-side
idempotence makes re-sends — e.g. overlapping with a later replay —
duplicates), but a ``validation`` rejection re-reads the file first: events
appended after the batch closed can complete the batch (ingest applies
canonical order within one request, so the missing entity snapshot joins),
which clears the rejection instead of retrying a body that can never
succeed.
"""

import sys
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep
from typing import Protocol

import httpx
from jernerics_schema import (
    PROTOCOL_VERSION,
    IngestError,
    IngestRequest,
    TrackingEvent,
)
from jernerics_schema.ingest import MAX_EVENTS_PER_REQUEST

from .jsonl_io import read_cursor, scan_events, write_cursor

RETRY_BASE_INTERVAL = 0.5
RETRY_MAX_WAIT = 10.0
MAX_ERROR_BODY_CHARS = 400


class TransportResponse(Protocol):
    status_code: int
    content: bytes


class Transport(Protocol):
    def post(
        self,
        url: str,
        *,
        content: str,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> TransportResponse: ...


class HttpTransport:
    """httpx POST wrapper; tests substitute fakes with the same shape."""

    def post(
        self,
        url: str,
        *,
        content: str,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> httpx.Response:
        return httpx.post(url, content=content, headers=headers, timeout=timeout)


class StreamClient:
    def __init__(
        self,
        base_url: str,
        path: Path,
        api_key: str | None = None,
        poll_interval: float = 0.5,
        flush_timeout: float = 60.0,
        send_deadline: float = 30.0,
        max_retry_time: float = 300.0,
        batch_size: int = MAX_EVENTS_PER_REQUEST,
        batch_window: float = 0.5,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.path = path
        self.api_key = api_key
        self.poll_interval = poll_interval
        self.flush_timeout = flush_timeout
        self.send_deadline = send_deadline
        self.max_retry_time = max_retry_time
        self.batch_size = batch_size
        self.batch_window = batch_window
        self.transport = transport if transport is not None else HttpTransport()
        self._headers = {"content-type": "application/json"}
        if api_key:
            self._headers["authorization"] = f"Bearer {api_key}"
        self._stop = Event()
        self._drain_deadline: float | None = None
        self._thread = Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        """Stop shipping: drain what is readable, bounded by flush_timeout."""
        self._drain_deadline = monotonic() + self.flush_timeout
        self._stop.set()
        if self._thread.ident is not None:
            self._thread.join(timeout=self.flush_timeout)
            if self._thread.is_alive():
                print(
                    "Warning: shipper did not finish within flush timeout. "
                    "Unacked events stay in the JSONL for replay.",
                    file=sys.stderr,
                )

    def _run(self) -> None:
        try:
            self._ship_loop()
        except Exception as exc:
            print(f"jernerics: tracking shipper stopped: {exc!r}", file=sys.stderr)

    def _ship_loop(self) -> None:
        url = f"{self.base_url}/ingest"
        offset = read_cursor(self.path)
        pending: list[tuple[TrackingEvent, int]] = []
        while True:
            if not pending:
                events, offset = scan_events(self.path, offset, self.batch_size)
                if not events and self._stop.is_set():
                    # A write can land between the scan above and the stop
                    # check below; anything written before join() set stop
                    # must still ship, so take one more look before giving up.
                    events, offset = scan_events(self.path, offset, self.batch_size)
                if not events:
                    if self._stop.is_set():
                        return
                    self._stop.wait(self.poll_interval)
                    continue
                pending = events
            if not self._stop.is_set():
                window_deadline = monotonic() + self.batch_window
                while len(pending) < self.batch_size:
                    # Always rescan before closing the window: a wait that
                    # runs to the deadline must not strand events written
                    # while we slept (a prefix batch can reference events
                    # the server has not seen yet and stick retrying).
                    events, offset = scan_events(
                        self.path, offset, self.batch_size - len(pending)
                    )
                    pending.extend(events)
                    remaining = window_deadline - monotonic()
                    if remaining <= 0:
                        break
                    self._stop.wait(min(remaining, self.poll_interval))
                    if self._stop.is_set():
                        break
            batch = pending[: self.batch_size]
            shipped = self._ship_batch(url, batch)
            if shipped is None:
                return
            write_cursor(self.path, shipped[-1][1])
            offset = max(offset, shipped[-1][1])
            pending = pending[len(shipped) :]

    def _ship_batch(
        self, url: str, batch: list[tuple[TrackingEvent, int]]
    ) -> list[tuple[TrackingEvent, int]] | None:
        body = self._request_body(batch)
        retry_count = 0
        first_failure: float | None = None
        while True:
            if self._drain_exceeded():
                print(
                    f"Warning: flush timeout reached with {len(batch)} events "
                    "unacked; leaving them for replay.",
                    file=sys.stderr,
                )
                return None
            try:
                response = self.transport.post(
                    url,
                    content=body,
                    headers=self._headers,
                    timeout=self.send_deadline,
                )
                if 200 <= response.status_code < 300:
                    return batch
                detail = f"HTTP {response.status_code}"
                if retry_count == 0:
                    error_body = (
                        response.content[:MAX_ERROR_BODY_CHARS]
                        .decode("utf-8", "replace")
                        .strip()
                    )
                    if error_body:
                        detail = f"{detail}: {error_body}"
                regrown = self._regrow_for_validation(response, batch)
                if regrown is not None:
                    batch = regrown
                    body = self._request_body(batch)
                    continue
            except httpx.HTTPError as exc:
                detail = repr(exc)
            now = monotonic()
            if first_failure is None:
                first_failure = now
            if now - first_failure > self.max_retry_time:
                print(
                    f"Warning: exceeded max retry time ({self.max_retry_time}s). "
                    f"Leaving {len(batch)} events for replay.",
                    file=sys.stderr,
                )
                return None
            wait_time = min(self.poll_interval * 2**retry_count, RETRY_MAX_WAIT)
            retry_count += 1
            print(
                f"Failed to ship batch ({detail}); "
                f"retry {retry_count} in {wait_time:.1f}s ...",
                file=sys.stderr,
            )
            if self._stop.is_set():
                sleep(wait_time)
            else:
                self._stop.wait(wait_time)

    def _regrow_for_validation(
        self, response: TransportResponse, batch: list[tuple[TrackingEvent, int]]
    ) -> list[tuple[TrackingEvent, int]] | None:
        """Re-read the file when the server names an unmet ordering
        precondition, absorbing events appended since the batch closed.
        Returns the grown batch, or ``None`` when the rejection is not a
        ``validation`` one or nothing new is readable.
        """
        if response.status_code != 409:
            return None
        try:
            error = IngestError.model_validate_json(response.content)
        except ValueError:
            return None
        if error.error != "validation":
            return None
        events, _ = scan_events(self.path, read_cursor(self.path), self.batch_size)
        if len(events) <= len(batch):
            return None
        return events

    def _request_body(self, batch: list[tuple[TrackingEvent, int]]) -> str:
        return IngestRequest(
            protocol_version=PROTOCOL_VERSION,
            events=[event for event, _ in batch],
        ).model_dump_json()

    def _drain_exceeded(self) -> bool:
        return (
            self._stop.is_set()
            and self._drain_deadline is not None
            and monotonic() > self._drain_deadline
        )
