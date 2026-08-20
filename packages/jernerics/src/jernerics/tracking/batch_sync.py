"""Replay unshipped JSONL events to the tracking server in acknowledged batches.

Each file ships from its durable byte cursor in ``IngestRequest`` batches;
the cursor advances only after a 2xx acknowledgement. Overlap with the live
shipper (or a repeated replay) is safe: the server treats re-sent events as
duplicates. Files whose batches cannot be shipped report a per-file error and
keep their cursor — no data loss.
"""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from jernerics_schema import PROTOCOL_VERSION, IngestRequest
from jernerics_schema.ingest import MAX_EVENTS_PER_REQUEST

from .jsonl_io import cursor_path, read_cursor, scan_events, write_cursor
from .stream_client import (
    RETRY_BASE_INTERVAL,
    RETRY_MAX_WAIT,
    HttpTransport,
    Transport,
)


@dataclass
class FileResult:
    path: Path
    events_sent: int = 0
    events_total: int = 0
    error: str | None = None


@dataclass
class ReplayResult:
    files_processed: int = 0
    events_sent: int = 0
    events_failed: int = 0
    errors: list[str] = field(default_factory=list)


def _post_with_retries(
    url: str,
    body: str,
    headers: dict[str, str],
    transport: Transport,
    max_retries: int,
    timeout: float = 30.0,
) -> None:
    retry_count = 0
    while True:
        try:
            response = transport.post(
                url, content=body, headers=headers, timeout=timeout
            )
            if 200 <= response.status_code < 300:
                return
            detail = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            detail = str(exc)
        retry_count += 1
        if retry_count >= max_retries:
            raise RuntimeError(
                f"ingest failed after {retry_count} attempts: {detail}"
            ) from None
        time.sleep(min(RETRY_BASE_INTERVAL * 2**retry_count, RETRY_MAX_WAIT))


def _replay_file(
    path: Path,
    base_url: str,
    api_key: str | None = None,
    max_retries: int = 10,
    transport: Transport | None = None,
) -> FileResult:
    transport = transport if transport is not None else HttpTransport()
    url = f"{base_url.rstrip('/')}/ingest"
    headers = {"content-type": "application/json"}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"

    result = FileResult(path=path)
    sent = 0
    try:
        result.events_total = len(scan_events(path, 0)[0])
        offset = read_cursor(path)
        while True:
            batch, offset = scan_events(path, offset, MAX_EVENTS_PER_REQUEST)
            if not batch:
                break
            body = IngestRequest(
                protocol_version=PROTOCOL_VERSION,
                events=[event for event, _ in batch],
            ).model_dump_json()
            _post_with_retries(url, body, headers, transport, max_retries)
            write_cursor(path, offset)
            sent += len(batch)
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        result.error = str(exc)
    result.events_sent = sent
    return result


def discover_jsonl_files(
    tracking_dir: Path,
    study: str | None = None,
) -> list[Path]:
    """Find all .jsonl event files under tracking_dir, optionally per study."""
    pattern = f"{study}/events/*.jsonl" if study else "*/events/*.jsonl"
    return sorted(tracking_dir.glob(pattern))


def replay_tracking(
    tracking_dir: Path,
    base_url: str,
    api_key: str | None = None,
    study: str | None = None,
    max_workers: int = 16,
    max_retries: int = 10,
    transport: Transport | None = None,
) -> ReplayResult:
    """
    Replay JSONL tracking files to the HTTP server.

    Idempotent: the server deduplicates by event id, so already-synced
    events (e.g. those a live StreamClient shipped) are reported as
    duplicates, and overlapping live + replay is safe.

    Args:
        tracking_dir: Host path containing study subdirectories with .jsonl files.
        base_url: Base URL of the tracking HTTP server.
        api_key: Optional bearer API key for authentication.
        study: Optional study name to scope the replay.
        max_workers: Thread pool size for concurrent sends.
        max_retries: Max retries per batch on HTTP failure.
        transport: Optional transport override (tests, in-process servers).
    """
    jsonl_files = discover_jsonl_files(tracking_dir, study)

    if not jsonl_files:
        print("No .jsonl files found.", file=sys.stderr)
        return ReplayResult()

    print(
        f"Replaying {len(jsonl_files)} file(s)"
        + (f" for study '{study}'" if study else "")
        + "...",
        file=sys.stderr,
    )

    aggregated = ReplayResult()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _replay_file, path, base_url, api_key, max_retries, transport
            ): path
            for path in jsonl_files
        }

        for future in futures:
            file_result = future.result()
            aggregated.files_processed += 1

            if file_result.error:
                aggregated.events_failed += (
                    file_result.events_total - file_result.events_sent
                )
                aggregated.errors.append(f"{file_result.path}: {file_result.error}")
                print(
                    f"  [FAIL] {file_result.path.name}: {file_result.error}",
                    file=sys.stderr,
                )
            else:
                aggregated.events_sent += file_result.events_sent
                if file_result.events_total > 0:
                    print(
                        f"  [{aggregated.files_processed}/{len(jsonl_files)}] "
                        f"{file_result.path.name} "
                        f"({file_result.events_sent}/"
                        f"{file_result.events_total} events)",
                        file=sys.stderr,
                    )

    print(
        f"Done. {aggregated.files_processed} files, "
        f"{aggregated.events_sent} events sent, "
        f"{aggregated.events_failed} failures.",
        file=sys.stderr,
    )

    if not aggregated.errors:
        for path in jsonl_files:
            path.unlink(missing_ok=True)
            cursor_path(path).unlink(missing_ok=True)
        print(
            f"Deleted {len(jsonl_files)} synced .jsonl file(s).",
            file=sys.stderr,
        )

    return aggregated
