import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx
from jernerics.tracking.artifact_manifest import manifest_cursor_path
from jernerics.tracking.blob_uploader import upload_pending_blobs


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b""


@dataclass
class RecordingTransport:
    responses: list[int]
    calls: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    headers: dict = field(default_factory=dict)
    fail_with: Exception | None = None

    def put(self, url, *, content, headers, timeout):
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(url.rsplit("/", 1)[-1])
        self.urls.append(url)
        self.headers.update(headers or {})
        content.read()
        return FakeResponse(self.responses.pop(0))


def _append_entry(manifest_path: Path, key: str, payload: bytes) -> str:
    artifact_id = uuid4().hex
    blob = manifest_path.parent / f"{artifact_id}.bin"
    blob.write_bytes(payload)
    with open(manifest_path, "a") as f:
        f.write(
            json.dumps({"artifact_id": artifact_id, "key": key, "path": str(blob)})
            + "\n"
        )
    return artifact_id


def _cursor(manifest_path: Path) -> str:
    return manifest_cursor_path(manifest_path).read_text()


class TestUploadPendingBlobs:
    def test_uploads_each_pending_blob(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        first = _append_entry(manifest_path, "a", b"one")
        second = _append_entry(manifest_path, "b", b"two")
        transport = RecordingTransport(responses=[200, 202])

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=transport
        )

        assert (result.uploaded, result.skipped_conflict, result.failed) == (2, 0, 0)
        assert transport.calls == [first, second]
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)

    def test_cursor_advances_only_after_success(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        _append_entry(manifest_path, "a", b"one")
        second = _append_entry(manifest_path, "b", b"two")
        upload_pending_blobs(
            "http://srv",
            "key",
            [manifest_path],
            transport=RecordingTransport(responses=[200, 500]),
        )

        first_end = len(manifest_path.read_text().splitlines()[0]) + 1
        assert _cursor(manifest_path) == str(first_end)

        retry = RecordingTransport(responses=[200])
        result = upload_pending_blobs(
            "http://srv", "key", [manifest_path], transport=retry
        )
        assert retry.calls == [second]
        assert (result.uploaded, result.failed) == (1, 0)

    def test_failed_entry_is_retried_with_same_artifact_id(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one")
        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=RecordingTransport([503])
        )

        assert result.failed == 1
        assert not manifest_cursor_path(manifest_path).exists()

        retry = RecordingTransport(responses=[200])
        upload_pending_blobs("http://srv", None, [manifest_path], transport=retry)
        assert retry.calls == [artifact_id]

    def test_409_conflict_advances_and_never_retries(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        _append_entry(manifest_path, "a", b"one")
        _append_entry(manifest_path, "b", b"two")

        result = upload_pending_blobs(
            "http://srv",
            None,
            [manifest_path],
            transport=RecordingTransport([409, 200]),
        )

        assert (result.uploaded, result.skipped_conflict, result.failed) == (1, 1, 0)
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)
        again = RecordingTransport(responses=[])
        upload_pending_blobs("http://srv", None, [manifest_path], transport=again)
        assert again.calls == []

    def test_202_counts_as_done(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        _append_entry(manifest_path, "a", b"one")

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=RecordingTransport([202])
        )

        assert result.uploaded == 1
        assert _cursor(manifest_path) == str(manifest_path.stat().st_size)

    def test_network_failure_stops_manifest_and_leaves_cursor(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        _append_entry(manifest_path, "a", b"one")

        result = upload_pending_blobs(
            "http://srv",
            None,
            [manifest_path],
            transport=RecordingTransport([], fail_with=httpx.ConnectError("down")),
        )

        assert (result.uploaded, result.failed) == (0, 1)
        assert not manifest_cursor_path(manifest_path).exists()

    def test_missing_blob_file_fails_that_manifest(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        manifest_path.write_text(
            json.dumps(
                {"artifact_id": "c" * 32, "key": "gone", "path": "/nope/gone.bin"}
            )
            + "\n"
        )
        transport = RecordingTransport(responses=[])

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=transport
        )

        assert result.failed == 1
        assert transport.calls == []

    def test_legacy_lines_are_skipped(self, tmp_path):
        manifest_path = tmp_path / "0.manifest"
        artifact_id = _append_entry(manifest_path, "a", b"one")
        with open(manifest_path, "a") as f:
            f.write(json.dumps({"key": "legacy", "path": "/old/legacy.pt"}) + "\n")

        result = upload_pending_blobs(
            "http://srv", None, [manifest_path], transport=RecordingTransport([200])
        )

        assert result.uploaded == 1
        assert result.failed == 0

        retry = RecordingTransport(responses=[200])
        upload_pending_blobs("http://srv", None, [manifest_path], transport=retry)
        assert retry.calls == []

    def test_multiple_manifests(self, tmp_path):
        first_path = tmp_path / "0.manifest"
        second_path = tmp_path / "1.manifest"
        _append_entry(first_path, "a", b"one")
        _append_entry(second_path, "b", b"two")

        result = upload_pending_blobs(
            "http://srv",
            None,
            [first_path, second_path],
            transport=RecordingTransport([200, 200]),
        )

        assert result.uploaded == 2


def test_bearer_header_sent_when_api_key_present(tmp_path):
    manifest_path = tmp_path / "0.manifest"
    _append_entry(manifest_path, "a", b"one")
    transport = RecordingTransport(responses=[200])

    upload_pending_blobs("http://srv", "secret", [manifest_path], transport=transport)

    assert transport.headers == {"authorization": "Bearer secret"}


def test_base_url_slashes_normalized(tmp_path):
    manifest_path = tmp_path / "0.manifest"
    _append_entry(manifest_path, "a", b"one")
    transport = RecordingTransport(responses=[200])

    upload_pending_blobs("http://srv/", None, [manifest_path], transport=transport)

    assert transport.urls[0].startswith("http://srv/artifact/")
