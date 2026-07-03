"""Replay orphaned JSONL tracking files to the HTTP server."""

import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .jsonl_io import TrackingReader

# Default retry settings (matches StreamClient).
_RETRY_BASE_INTERVAL = 0.5
_RETRY_MAX_WAIT = 10.0


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


def _replay_file(
    path: Path,
    base_url: str,
    api_key: str | None,
    max_retries: int = 10,
) -> FileResult:
    result = FileResult(path=path)
    headers = {"authorization": f"Bearer {api_key}"} if api_key else None

    try:
        with TrackingReader(path) as reader:
            envelopes = list(reader)
        result.events_total = len(envelopes)

        for event in envelopes:
            retry_count = 0
            while True:
                try:
                    response = httpx.post(
                        f"{base_url}/ingest",
                        json=event,
                        headers=headers,
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    retry_count += 1
                    if retry_count >= max_retries:
                        raise
                    wait_time = min(
                        _RETRY_BASE_INTERVAL * 2**retry_count,
                        _RETRY_MAX_WAIT,
                    )
                    time.sleep(wait_time)
            result.events_sent += 1
    except (httpx.HTTPError, OSError, ValueError) as e:
        result.error = str(e)

    return result


def discover_jsonl_files(
    tracking_dir: Path,
    study: str | None = None,
) -> list[Path]:
    """Find all .jsonl files under tracking_dir, optionally scoped to one study."""
    pattern = f"{study}/events/*.jsonl" if study else "*/events/*.jsonl"
    return sorted(tracking_dir.glob(pattern))


def replay_tracking(
    tracking_dir: Path,
    base_url: str,
    api_key: str | None = None,
    study: str | None = None,
    max_workers: int = 16,
    max_retries: int = 10,
) -> ReplayResult:
    """
    Replay JSONL tracking files to the HTTP server.

    Idempotent: the server uses INSERT OR IGNORE, so already-synced
    events (e.g. those a live StreamClient already shipped) are silently
    dropped, and overlapping live + replay is safe.

    Args:
        tracking_dir: Host path containing study subdirectories with .jsonl files.
        base_url: Base URL of the tracking HTTP server.
        api_key: Optional bearer API key for authentication.
        study: Optional study name to scope the replay.
        max_workers: Thread pool size for concurrent sends.
        max_retries: Max retries per event on HTTP failure.
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
            executor.submit(_replay_file, path, base_url, api_key, max_retries): path
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
            path.unlink()
        print(
            f"Deleted {len(jsonl_files)} synced .jsonl file(s).",
            file=sys.stderr,
        )

    return aggregated


def discover_manifest_files(
    tracking_dir: Path,
    study: str | None = None,
) -> list[Path]:
    """Find all .manifest files under tracking_dir."""
    pattern = f"{study}/artifacts/*.manifest" if study else "*/artifacts/*.manifest"
    return sorted(tracking_dir.glob(pattern))


def sync_artifacts(
    tracking_dir: Path,
    upload_fn,
    project: str,
    study: str,
    trial_id: int | None = None,
) -> None:
    """Upload artifacts from manifests.

    Reads manifest entries, uploads via upload_fn, advances cursor.
    """
    from jernerics.tracking.artifact_manifest import ArtifactManifest

    manifest_files = discover_manifest_files(tracking_dir, study)

    for manifest_path in manifest_files:
        cursor_path = manifest_path.with_suffix(".cursor")
        manifest = ArtifactManifest(manifest_path, cursor_path=cursor_path)
        entries = manifest.read_from_cursor()

        for entry in entries:
            key = entry["key"]
            local_path = entry["path"]
            # Derive trial_id from manifest filename (e.g. "0.manifest" -> 0)
            manifest_trial = int(manifest_path.stem)
            s3_key = f"{project}/{study}/{manifest_trial}/{key}"
            upload_fn(s3_key, local_path)

        # Advance cursor to end of file after successful upload
        if entries and manifest_path.exists():
            manifest.advance_cursor(manifest_path.stat().st_size)
