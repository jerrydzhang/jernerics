"""Uploads manifest-declared artifact blobs to the tracking server.

The manifest cursor advances only after a terminal outcome per entry:
2xx (stored or awaiting declaration) and 409 (the server holds
different bytes for that artifact id) both count as done; anything
else — network failure, other HTTP status, unreadable file — stops
that manifest with the cursor left before the failing entry, so the
next sweep re-uploads the same artifact ids.
"""

import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .artifact_manifest import ArtifactManifest
from .stream_client import TransportResponse


class BlobTransport(Protocol):
    def put(
        self,
        url: str,
        *,
        content: Iterable[bytes],
        headers: dict[str, str] | None,
        timeout: float,
    ) -> TransportResponse: ...


class HttpBlobTransport:
    """httpx PUT wrapper; streams file-like content; tests substitute fakes."""

    def put(
        self,
        url: str,
        *,
        content: Iterable[bytes],
        headers: dict[str, str] | None,
        timeout: float,
    ) -> httpx.Response:
        return httpx.put(url, content=content, headers=headers, timeout=timeout)


@dataclass
class BlobUploadResult:
    uploaded: int = 0
    skipped_conflict: int = 0
    failed: int = 0


def upload_pending_blobs(
    base_url: str,
    api_key: str | None,
    manifest_paths: Iterable[Path],
    *,
    timeout: float = 300.0,
    transport: BlobTransport | None = None,
) -> BlobUploadResult:
    transport = transport if transport is not None else HttpBlobTransport()
    url = f"{base_url.rstrip('/')}/artifact"
    headers = {}
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    result = BlobUploadResult()
    for manifest_path in manifest_paths:
        manifest = ArtifactManifest(Path(manifest_path))
        try:
            for entry in manifest.read_from_cursor():
                with open(entry.path, "rb") as blob:
                    response = transport.put(
                        f"{url}/{entry.artifact_id}",
                        content=blob,
                        headers=headers,
                        timeout=timeout,
                    )
                if 200 <= response.status_code < 300:
                    result.uploaded += 1
                elif response.status_code == 409:
                    result.skipped_conflict += 1
                    print(
                        f"jernerics: server holds different bytes for artifact "
                        f"{entry.artifact_id} (key {entry.key!r}); keeping the "
                        "server's copy",
                        file=sys.stderr,
                    )
                else:
                    result.failed += 1
                    print(
                        f"jernerics: blob upload for {entry.artifact_id} failed "
                        f"with HTTP {response.status_code}",
                        file=sys.stderr,
                    )
                    break
                manifest.advance_cursor(entry.end_offset)
        except (httpx.HTTPError, OSError) as exc:
            result.failed += 1
            print(
                f"jernerics: blob upload stopped at {manifest_path}: {exc}",
                file=sys.stderr,
            )
    return result


def sweep_manifest_blobs(
    tracking_dir: str | Path, base_url: str, api_key: str | None
) -> None:
    """Upload every study's pending manifest blobs under the tracking root."""
    tracking_root = Path(tracking_dir).parent
    manifests = sorted(tracking_root.glob("*/artifacts/*.manifest"))
    if not manifests:
        return
    result = upload_pending_blobs(base_url, api_key, manifests)
    print(
        f"Blobs: {len(manifests)} manifest(s) swept — {result.uploaded} "
        f"uploaded, {result.skipped_conflict} conflict(s) skipped, "
        f"{result.failed} failed.",
        file=sys.stderr,
    )
