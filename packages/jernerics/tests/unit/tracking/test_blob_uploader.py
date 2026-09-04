import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
from jernerics.tracking.artifact_manifest import manifest_cursor_path
from jernerics.tracking.blob_uploader import BlobProbe, upload_pending_blobs


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b""
    headers: dict = field(default_factory=dict)


@dataclass
class FakeBlobServer:
    """PUT/GET /artifact stand-in mirroring the real route semantics.

    GET serves a blob only once its declaration is ingested and the
    bytes are stored; an undeclared PUT answers 202 (awaiting
    declaration), a conflicting re-upload answers 409.
    """

    blobs: dict[str, bytes] = field(default_factory=dict)
    declared: set[str] = field(default_factory=set)
    puts: list[str] = field(default_factory=list)
    gets: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    put_status_override: int | None = None
    etag_override: str | None = None
    fail_put_with: Exception | None = None
    fail_get_with: Exception | None = None

    def put(self, url, *, content, headers, timeout):
        if self.fail_put_with is not None:
            raise self.fail_put_with
        artifact_id = url.rsplit("/", 1)[-1]
        self.puts.append(artifact_id)
        self.headers.update(headers or {})
        if self.put_status_override is not None:
            return FakeResponse(self.put_status_override)
        payload = b"".join(content)
        existing = self.blobs.get(artifact_id)
        if existing is not None:
            return FakeResponse(200 if existing == payload else 409)
        self.blobs[artifact_id] = payload
        return FakeResponse(200 if artifact_id in self.declared else 202)

    def get(self, url, *, headers, timeout):
        if self.fail_get_with is not None:
            raise self.fail_get_with
        artifact_id = url.rsplit("/", 1)[-1]
        self.gets.append(artifact_id)
        self.headers.update(headers or {})
        payload = self.blobs.get(artifact_id)
        if artifact_id not in self.declared or payload is None:
            return BlobProbe(404, None)
        etag = self.etag_override or hashlib.sha256(payload).hexdigest()
        return BlobProbe(200, f'"{etag}"')


def _append_entry(
    manifest_path: Path, key: str, payload: bytes, *, staged: bool = False
) -> str:
    artifact_id = uuid4().hex
    blob = manifest_path.parent / f"{artifact_id}.bin"
    blob.write_bytes(payload)
    entry: dict[str, object] = {
        "artifact_id": artifact_id,
        "key": key,
        "path": str(blob),
    }
    if staged:
        entry["staged"] = True
    with open(manifest_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return artifact_id


def _cursor(manifest_path: Path) -> str:
    return manifest_cursor_path(manifest_path).read_text()


class TestUploadPendingBlobs:
    def test_uploads_each_pending_blob(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        first = _append_entry(manifest_path, "a", b"one")
        second = _append_entry(manifest_path, "b", b"two")
        server = FakeBlobServer(declared={first, second})

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (2, 0, 0)
        assert server.puts == [first, second]
        assert server.gets == [first, second]
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)

    def test_cursor_advances_only_after_receipt(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        first = _append_entry(manifest_path, "a", b"one")
        second = _append_entry(manifest_path, "b", b"two")
        server = FakeBlobServer(declared={first})

        upload_pending_blobs("http://srv", None, [manifest_path], transport=server)

        first_end = len(manifest_path.read_text().splitlines()[0]) + 1
        assert _cursor(manifest_path) == str(first_end)

        server.declared.add(second)
        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )
        assert server.puts == [first, second, second]
        assert (result.uploaded, result.pending) == (1, 0)

    def test_202_without_declaration_keeps_blob_and_retries(self, tmp_path, capsys):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer()

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (0, 1, 0)
        assert not manifest_cursor_path(manifest_path).exists()
        assert blob.exists()
        assert artifact_id in capsys.readouterr().err

        server.declared.add(artifact_id)
        retry_result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )
        assert (retry_result.uploaded, retry_result.pending) == (1, 0)
        assert not blob.exists()
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)

    def test_409_conflict_keeps_blob_for_retry(self, tmp_path, capsys):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer(
            blobs={artifact_id: b"different"},
            declared={artifact_id},
        )

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (0, 1, 0)
        assert blob.exists()
        assert artifact_id in capsys.readouterr().err

        again = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )
        assert server.puts == [artifact_id, artifact_id]
        assert again.pending == 1

    def test_dedupe_hit_counts_as_receipt(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer(blobs={artifact_id: b"one"}, declared={artifact_id})

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (1, 0, 0)
        assert not blob.exists()
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)

    def test_declaration_with_lost_server_blob_is_refilled(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer(declared={artifact_id})

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (1, 0, 0)
        assert not blob.exists()
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)

    def test_503_fails_manifest_and_keeps_cursor(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer(put_status_override=503)

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (0, 0, 1)
        assert not manifest_cursor_path(manifest_path).exists()
        assert blob.exists()

        server.put_status_override = None
        server.declared.add(artifact_id)
        retry = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )
        assert retry.uploaded == 1
        assert not blob.exists()

    def test_network_failure_stops_manifest_and_leaves_cursor(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        _append_entry(manifest_path, "a", b"one")

        result = upload_pending_blobs(
            "http://srv",
            None,
            [manifest_path],
            transport=FakeBlobServer(fail_put_with=httpx.ConnectError("down")),
        )

        assert (result.uploaded, result.failed) == (0, 1)
        assert not manifest_cursor_path(manifest_path).exists()

    def test_probe_outage_keeps_blob(self, tmp_path, capsys):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer(
            declared={artifact_id}, fail_get_with=httpx.ConnectError("flap")
        )

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (0, 1, 0)
        assert blob.exists()
        assert not manifest_cursor_path(manifest_path).exists()
        assert artifact_id in capsys.readouterr().err

    def test_etag_mismatch_keeps_blob(self, tmp_path, capsys):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer(declared={artifact_id}, etag_override="deadbeef")

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (0, 1, 0)
        assert blob.exists()
        assert "etag mismatch" in capsys.readouterr().err

    def test_unconfirmed_entry_stops_manifest(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        first = _append_entry(manifest_path, "a", b"one")
        second = _append_entry(manifest_path, "b", b"two")
        third = _append_entry(manifest_path, "c", b"three")
        server = FakeBlobServer(declared={first, third})

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert (result.uploaded, result.pending, result.failed) == (1, 1, 0)
        assert server.puts == [first, second]
        assert server.gets == [first, second]

        first_end = len(manifest_path.read_text().splitlines()[0]) + 1
        assert _cursor(manifest_path) == str(first_end)

        server.declared.add(second)
        retry = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )
        assert server.puts == [first, second, second, third]
        assert (retry.uploaded, retry.pending) == (2, 0)

    def test_staged_blob_survives_until_receipt(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one", staged=True)
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer()

        upload_pending_blobs("http://srv", None, [manifest_path], transport=server)
        assert blob.exists()

        server.declared.add(artifact_id)
        upload_pending_blobs("http://srv", None, [manifest_path], transport=server)
        assert not blob.exists()

    def test_unmarked_entry_is_never_unlinked(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one")
        blob = manifest_path.parent / f"{artifact_id}.bin"
        server = FakeBlobServer(declared={artifact_id})

        upload_pending_blobs("http://srv", None, [manifest_path], transport=server)

        assert blob.exists()
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)

    def test_legacy_lines_are_skipped(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one")
        with open(manifest_path, "a") as f:
            f.write(json.dumps({"key": "legacy", "path": "/old/legacy.pt"}) + "\n")
        server = FakeBlobServer(declared={artifact_id})

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert result.uploaded == 1
        assert result.failed == 0

        retry = FakeBlobServer(declared={artifact_id})
        upload_pending_blobs("http://srv", None, [manifest_path], transport=retry)
        assert retry.puts == []

    def test_missing_blob_file_fails_that_manifest(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        manifest_path.write_text(
            json.dumps(
                {"artifact_id": "c" * 32, "key": "gone", "path": "/nope/gone.bin"}
            )
            + "\n"
        )
        server = FakeBlobServer()

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=server
        )

        assert result.failed == 1
        assert server.puts == []

    def test_multiple_manifests(self, tmp_path):
        first_path = tmp_path / "0.manifest"
        second_path = tmp_path / "1.manifest"
        first = _append_entry(first_path, "a", b"one")
        second = _append_entry(second_path, "b", b"two")
        server = FakeBlobServer(declared={first, second})

        result = upload_pending_blobs(
            "http://srv", None, [first_path, second_path], transport=server
        )

        assert result.uploaded == 2


def test_bearer_header_sent_when_api_key_present(tmp_path):
    manifest_path = tmp_path / "0.manifest"
    artifact_id = _append_entry(manifest_path, "a", b"one")
    server = FakeBlobServer(declared={artifact_id})

    upload_pending_blobs("http://srv", "secret", [manifest_path], transport=server)

    assert server.headers == {"authorization": "Bearer secret"}


def test_base_url_slashes_normalized(tmp_path):
    manifest_path = tmp_path / "0.manifest"
    artifact_id = _append_entry(manifest_path, "a", b"one")
    server = FakeBlobServer(declared={artifact_id})

    upload_pending_blobs("http://srv/", None, [manifest_path], transport=server)

    assert server.puts == [artifact_id]
    assert server.gets == [artifact_id]
