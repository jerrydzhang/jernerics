"""Replay unshipped JSONL events to the tracking server in acknowledged batches.

Each file ships from its durable byte cursor in ``IngestRequest`` batches;
the cursor advances only after a 2xx acknowledgement. Overlap with the live
shipper (or a repeated replay) is safe: the server treats re-sent events as
duplicates. Files whose batches cannot be shipped report a per-file error and
keep their cursor — no data loss. A shipped file is unlinked only when no
live writer holds it and the cursor covers its current EOF; anything else
stays for the next replay.
"""

import fcntl
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from time import sleep

import httpx
from jernerics_schema import (
    PROTOCOL_VERSION,
    ConflictRecord,
    IngestError,
    IngestRequest,
    IngestResponse,
    TrackingEvent,
    TrialId,
)
from jernerics_schema.ingest import MAX_EVENTS_PER_REQUEST

from .jsonl_io import (
    cursor_lock_path,
    cursor_path,
    read_cursor,
    scan_events,
    write_cursor,
)
from .stream_client import (
    RETRY_BASE_INTERVAL,
    RETRY_MAX_WAIT,
    HttpTransport,
    Transport,
    TransportResponse,
)


@dataclass
class FileResult:
    path: Path
    events_sent: int = 0
    events_total: int = 0
    error: str | None = None
    conflicts: list[ConflictRecord] = field(default_factory=list)


@dataclass
class ReplayResult:
    files_processed: int = 0
    events_sent: int = 0
    events_failed: int = 0
    errors: list[str] = field(default_factory=list)
    conflicts: list[ConflictRecord] = field(default_factory=list)


def _structured_conflict(response: TransportResponse) -> IngestError | None:
    """Parse a structured 409 body that names a permanently rejected event.

    Only ``conflict``-coded rejections qualify: they disagree with
    immutable server state, so re-sending can never clear them.
    ``validation`` rejections (unknown entity references) may be a
    transient cross-file ordering race inside one replay wave — the
    submission files of a wave ship concurrently — and keep the retry
    path.
    """
    if response.status_code != 409:
        return None
    try:
        error = IngestError.model_validate_json(response.content)
    except ValueError:
        return None
    if error.error != "conflict" or error.event_index is None:
        return None
    return error


def _named_event(
    error: IngestError, batch: list[tuple[TrackingEvent, int]]
) -> tuple[int, int, TrialId] | None:
    """Locate the event a structured 409 names for drop-and-report.

    The index must address the batch the 409 was returned for, and the
    event must carry a ``trial_id`` so the conflict is recordable in a
    ``ConflictRecord``. Returns ``(index, end_offset, trial_id)``.
    """
    index = error.event_index
    if index is None or not 0 <= index < len(batch):
        return None
    event, end = batch[index]
    trial_id = getattr(event, "trial_id", None)
    if trial_id is None:
        return None
    return index, end, trial_id


def _post_with_retries(
    url: str,
    body: str,
    headers: dict[str, str],
    transport: Transport,
    max_retries: int,
    timeout: float = 30.0,
) -> TransportResponse:
    retry_count = 0
    while True:
        try:
            response = transport.post(
                url, content=body, headers=headers, timeout=timeout
            )
            if 200 <= response.status_code < 300:
                return response
            if _structured_conflict(response) is not None:
                return response
            detail = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            detail = str(exc)
        retry_count += 1
        if retry_count >= max_retries:
            raise RuntimeError(
                f"ingest failed after {retry_count} attempts: {detail}"
            ) from None
        sleep(min(RETRY_BASE_INTERVAL * 2**retry_count, RETRY_MAX_WAIT))


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
        acked = offset
        while True:
            batch, offset = scan_events(path, offset, MAX_EVENTS_PER_REQUEST)
            if not batch:
                break
            while batch:
                body = IngestRequest(
                    protocol_version=PROTOCOL_VERSION,
                    events=[event for event, _ in batch],
                ).model_dump_json()
                response = _post_with_retries(
                    url, body, headers, transport, max_retries
                )
                error = _structured_conflict(response)
                if error is not None:
                    named = _named_event(error, batch)
                    if named is None:
                        raise RuntimeError(f"ingest rejected: {error.detail}")
                    index, end, trial_id = named
                    batch.pop(index)
                    # A structured 409 is deterministic: re-sending can
                    # never clear it. Drop the event, record the
                    # conflict, and acknowledge past its bytes so the
                    # rest of the file still ships — an event whose
                    # immutable facts disagree is permanently
                    # unshippable, so counting it acknowledged (and
                    # deleting the file once fully covered) is correct.
                    acked = max(acked, end)
                    write_cursor(path, acked)
                    result.conflicts.append(
                        ConflictRecord(
                            trial_id=trial_id,
                            kind=error.error,
                            detail=(
                                f"{path.name} event {error.event_id}: {error.detail}"
                            ),
                        )
                    )
                    continue
                result.conflicts.extend(
                    IngestResponse.model_validate_json(response.content).conflicts
                )
                acked = max(acked, batch[-1][1])
                write_cursor(path, acked)
                sent += len(batch)
                break
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        result.error = str(exc)
    result.events_sent = sent
    return result


def ship_events_file(
    path: Path,
    base_url: str,
    api_key: str | None = None,
    *,
    max_retries: int = 3,
    transport: Transport | None = None,
) -> bool:
    """Best-effort immediate ship of one events file, cursor-honoring.

    Deploy-time use: land sweep/submission/job (or checker snapshot)
    events on the server the moment they are written, so live trial
    streams validate from their first batch instead of 409-retrying
    until the post-hook replay. Reuses the replay batch/cursor logic;
    never deletes the file. A missing file is a silent no-op (remote
    backends write it on the host, not here); any failure only leaves a
    stderr note — the post-hook replay remains the delivery guarantee
    and the cursor stays where it was.
    """
    if not path.is_file():
        return False
    try:
        result = _replay_file(path, base_url, api_key, max_retries, transport)
    except Exception as e:
        print(
            f"jernerics: immediate ship of {path.name} failed: {e!r}; "
            "the post-hook replay will deliver it.",
            file=sys.stderr,
        )
        return False
    if result.error:
        print(
            f"jernerics: immediate ship of {path.name} failed: "
            f"{result.error}; the post-hook replay will deliver it.",
            file=sys.stderr,
        )
        return False
    return True


def discover_jsonl_files(
    tracking_dir: Path,
    study: str | None = None,
) -> list[Path]:
    """Find event and submission .jsonl files, optionally per study."""
    prefix = study if study is not None else "*"
    patterns = (f"{prefix}/events/*.jsonl", f"{prefix}/submission/*.jsonl")
    return sorted({path for pattern in patterns for path in tracking_dir.glob(pattern)})


def _delete_shipped(path: Path) -> bool:
    """Delete a shipped file and its cursor sidecars under an exclusive lock,
    only when no live writer holds it (a ``TrackingWriter`` holds a shared
    flock for its lifetime) and every byte of the current file is
    acknowledged — appends that landed after this replay's scan stay for the
    next replay. A missing file leaves only cursor cleanup. The unlink
    happens while the exclusive lock is still held so a writer opening the
    path cannot end up on an inode that is about to be deleted.
    """
    if not path.exists():
        cursor_path(path).unlink(missing_ok=True)
        cursor_lock_path(path).unlink(missing_ok=True)
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    try:
        try:
            fully_acked = path.stat().st_size == read_cursor(path)
        except OSError:
            return False
        if not fully_acked:
            return False
        path.unlink(missing_ok=True)
        cursor_path(path).unlink(missing_ok=True)
        cursor_lock_path(path).unlink(missing_ok=True)
        return True
    finally:
        os.close(fd)


def replay_tracking(
    tracking_dir: Path,
    base_url: str,
    api_key: str | None = None,
    study: str | None = None,
    max_workers: int = 16,
    max_retries: int = 10,
    transport: Transport | None = None,
    skip: set[Path] | None = None,
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
        skip: Optional files to leave entirely untouched by this replay.
    """
    jsonl_files = [
        path
        for path in discover_jsonl_files(tracking_dir, study)
        if path not in (skip or set())
    ]

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

    # Submission-level files ship before per-trial event logs: sweeps and
    # retry parents must exist before trial event batches reference them.
    waves = [
        [path for path in jsonl_files if path.parent.name != "events"],
        [path for path in jsonl_files if path.parent.name == "events"],
    ]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for wave in waves:
            futures = {
                executor.submit(
                    _replay_file, path, base_url, api_key, max_retries, transport
                ): path
                for path in wave
            }
            for future in futures:
                file_result = future.result()
                aggregated.files_processed += 1
                aggregated.conflicts.extend(file_result.conflicts)

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
        deleted = 0
        skipped = 0
        for path in jsonl_files:
            if _delete_shipped(path):
                deleted += 1
            else:
                skipped += 1
        if deleted:
            print(f"Deleted {deleted} synced .jsonl file(s).", file=sys.stderr)
        if skipped:
            print(f"Skipped {skipped} file(s) still in use.", file=sys.stderr)

    return aggregated
