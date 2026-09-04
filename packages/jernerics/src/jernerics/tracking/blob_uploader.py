"""Uploads manifest-declared artifact blobs to the tracking server.

A staged blob is pruned only after the server confirms receipt of that
exact artifact: the PUT must return 2xx AND a follow-up GET probe on
``/artifact/{id}`` must return 200 — the server serves an artifact only
once its declaration and received-blob row persist — with an ETag
matching the uploaded bytes' sha256 when the server sends one. 409
(server holds different bytes) and every unconfirmed outcome leave the
blob on disk and the manifest cursor before the entry, counted as
pending in the sweep summary so the next sweep retries the upload.
Hard failures — network errors, other HTTP statuses, unreadable files —
stop that manifest at the failing entry the same way. Staged blobs
(lines marked ``"staged": true``, Jernerics-owned copies) are unlinked
once receipt is confirmed; caller-owned paths are never touched.
"""

import hashlib
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

import httpx

from .artifact_manifest import ArtifactManifest
from .stream_client import TransportResponse

_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class BlobProbe:
    status_code: int
    etag: str | None


class BlobTransport(Protocol):
    def put(
        self,
        url: str,
        *,
        content: Iterable[bytes],
        headers: dict[str, str] | None,
        timeout: float,
    ) -> TransportResponse: ...

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> BlobProbe: ...


class HttpBlobTransport:
    """httpx PUT/GET wrappers; tests substitute fakes with the same shape."""

    def put(
        self,
        url: str,
        *,
        content: Iterable[bytes],
        headers: dict[str, str] | None,
        timeout: float,
    ) -> httpx.Response:
        return httpx.put(url, content=content, headers=headers, timeout=timeout)

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> BlobProbe:
        with httpx.stream("GET", url, headers=headers, timeout=timeout) as response:
            return BlobProbe(response.status_code, response.headers.get("etag"))


@dataclass
class BlobUploadResult:
    uploaded: int = 0
    pending: int = 0
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
                    digest = hashlib.sha256()
                    response = transport.put(
                        f"{url}/{entry.artifact_id}",
                        content=_hashed_stream(blob, digest),
                        headers=headers,
                        timeout=timeout,
                    )
                if response.status_code == 409:
                    result.pending += 1
                    print(
                        f"jernerics: server holds different bytes for artifact "
                        f"{entry.artifact_id} (key {entry.key!r}); keeping the "
                        "local blob for a retry next sweep",
                        file=sys.stderr,
                    )
                    break
                if not 200 <= response.status_code < 300:
                    result.failed += 1
                    print(
                        f"jernerics: blob upload for {entry.artifact_id} failed "
                        f"with HTTP {response.status_code}",
                        file=sys.stderr,
                    )
                    break
                try:
                    probe = transport.get(
                        f"{url}/{entry.artifact_id}", headers=headers, timeout=timeout
                    )
                except (httpx.HTTPError, OSError) as exc:
                    result.pending += 1
                    print(
                        f"jernerics: receipt probe for artifact "
                        f"{entry.artifact_id} failed ({exc!r}); keeping the "
                        "local blob for a retry next sweep",
                        file=sys.stderr,
                    )
                    break
                if probe.status_code != 200 or not _etag_matches(
                    probe.etag, digest.hexdigest()
                ):
                    result.pending += 1
                    detail = (
                        "server serves different bytes (etag mismatch)"
                        if probe.status_code == 200
                        else f"no server receipt (probe HTTP {probe.status_code})"
                    )
                    print(
                        f"jernerics: {detail} for artifact {entry.artifact_id}; "
                        "keeping the local blob for a retry next sweep",
                        file=sys.stderr,
                    )
                    break
                result.uploaded += 1
                manifest.advance_cursor(entry.end_offset)
                if entry.staged:
                    _unlink_staged(Path(entry.path))
        except (httpx.HTTPError, OSError) as exc:
            result.failed += 1
            print(
                f"jernerics: blob upload stopped at {manifest_path}: {exc}",
                file=sys.stderr,
            )
    return result


def _hashed_stream(blob_file: BinaryIO, digest: "hashlib._Hash") -> Iterator[bytes]:
    for chunk in iter(lambda: blob_file.read(_CHUNK_BYTES), b""):
        digest.update(chunk)
        yield chunk


def _etag_matches(etag: str | None, sha256: str) -> bool:
    if etag is None:
        return True
    return etag.strip('"') == sha256


def _unlink_staged(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"jernerics: could not remove staged blob {path}: {exc}",
            file=sys.stderr,
        )


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
        f"confirmed, {result.pending} pending receipt, {result.failed} failed.",
        file=sys.stderr,
    )
