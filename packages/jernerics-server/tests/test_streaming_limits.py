"""Streaming size limits: metered /ingest bodies and bounded artifact PUTs."""

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionStartEvent,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
)
from jernerics_server import http as http_module
from jernerics_server.http import MAX_INGEST_BYTES, create_app
from jernerics_server.store import Store

T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
PAYLOAD = b"0123456789abcdef"
SHA_PAYLOAD = hashlib.sha256(PAYLOAD).hexdigest()


def at(offset_s: float) -> datetime:
    return T0 + timedelta(seconds=offset_s)


def rows(store: Store, sql: str, params: list | None = None) -> list[tuple]:
    _, data = store.query(sql, params or [])
    return data


def _chunks(body: bytes, chunk_size: int = 16):
    """Generator content: httpx sends it chunked, with no Content-Length."""
    for start in range(0, len(body), chunk_size):
        yield body[start : start + chunk_size]


def _make_env(tmp_path, *, max_artifact_bytes: int | None = None):
    store = Store(tmp_path / "limits.sqlite")
    root = tmp_path / "blobs"
    app = create_app(store, artifacts_root=root, max_artifact_bytes=max_artifact_bytes)
    return SimpleNamespace(store=store, root=root, client=TestClient(app))


@pytest.fixture
def env(tmp_path):
    return _make_env(tmp_path)


def _declare(
    env,
    artifact_id: uuid.UUID,
    *,
    sha256: str | None = SHA_PAYLOAD,
    size_bytes: int = len(PAYLOAD),
) -> None:
    """Ingest a minimal graph plus one artifact declaration."""
    sweep, trial, execution = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=at(0),
            project="proj",
            sweep_id=sweep,
            name=f"limit-{sweep.hex[:8]}",
            state="running",
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=at(1),
            trial_id=trial,
            sweep_id=sweep,
            number=0,
            state=TrialState.RUNNING,
            retry_root_trial_id=trial,
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=at(2),
            execution_id=execution,
            trial_id=trial,
            hostname="node01",
            started_at=at(2),
        ),
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=at(3),
            artifact_id=artifact_id,
            trial_id=trial,
            execution_id=execution,
            key="model",
            filename="model.bin",
            content_type="application/octet-stream",
            size_bytes=size_bytes,
            sha256=sha256,
            source="user",
        ),
    ]
    response = env.client.post(
        "/ingest",
        json={
            "protocol_version": PROTOCOL_VERSION,
            "events": [event.model_dump(mode="json") for event in events],
        },
    )
    assert response.status_code == 200, response.text


def _ingest_body(events: list) -> bytes:
    return json.dumps({"protocol_version": PROTOCOL_VERSION, "events": events}).encode()


def _sweep_event() -> dict:
    sweep = uuid.uuid4()
    return SweepSnapshotEvent(
        event_id=uuid.uuid4(),
        recorded_at=at(0),
        project="proj",
        sweep_id=sweep,
        name=f"limit-{sweep.hex[:8]}",
        state="running",
    ).model_dump(mode="json")


class TestIngestStreamLimit:
    def test_chunked_body_over_limit_is_413_and_ingests_nothing(self, env, monkeypatch):
        monkeypatch.setattr(http_module, "MAX_INGEST_BYTES", 64)
        body = _ingest_body([_sweep_event()])
        assert len(body) > 64

        response = env.client.post(
            "/ingest",
            content=_chunks(body),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 413
        error = response.json()
        assert error["error"] == "payload_too_large"
        assert error["detail"] == "request body exceeds the 64-byte ingest limit"
        assert rows(env.store, "SELECT COUNT(*) FROM sweeps") == [(0,)]
        queried = env.client.post("/query", json={"sql": "SELECT COUNT(*) FROM sweeps"})
        assert queried.json()["rows"] == [[0]]

    def test_chunked_body_exactly_at_limit_is_accepted(self, env, monkeypatch):
        base = _ingest_body([])
        limit = len(base) + 40
        monkeypatch.setattr(http_module, "MAX_INGEST_BYTES", limit)

        response = env.client.post(
            "/ingest",
            content=_chunks(base + b" " * (limit - len(base))),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 200
        assert response.json() == {
            "accepted": 0,
            "duplicates": 0,
            "conflicts": [],
        }

    def test_content_length_exactly_at_limit_is_accepted(self, env, monkeypatch):
        base = _ingest_body([])
        limit = len(base) + 40
        monkeypatch.setattr(http_module, "MAX_INGEST_BYTES", limit)

        response = env.client.post(
            "/ingest",
            content=base + b" " * (limit - len(base)),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 200

    def test_chunked_body_over_true_constant_is_413(self, env):
        response = env.client.post(
            "/ingest",
            content=_chunks(b" " * (MAX_INGEST_BYTES + 1)),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 413
        assert response.json()["error"] == "payload_too_large"
        assert rows(env.store, "SELECT COUNT(*) FROM sweeps") == [(0,)]


class TestArtifactDeclaredLimit:
    def test_upload_over_declared_size_is_413_and_leaves_no_trace(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id, size_bytes=len(PAYLOAD))

        response = env.client.put(
            f"/artifact/{artifact_id.hex}", content=b"x" * (len(PAYLOAD) + 1)
        )

        assert response.status_code == 413
        error = response.json()
        assert error["error"] == "payload_too_large"
        assert error["detail"] == (
            f"artifact body exceeds the {len(PAYLOAD)}-byte limit"
        )
        assert not (env.root / str(artifact_id)[:2]).exists()
        assert list((env.root / "tmp").iterdir()) == []
        assert rows(env.store, "SELECT COUNT(*) FROM artifact_blobs") == [(0,)]
        assert rows(env.store, "SELECT received_ns IS NULL FROM artifacts") == [(True,)]
        missing = env.client.get(f"/artifact/{artifact_id.hex}")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "blob not received"

    def test_reupload_longer_than_stored_blob_is_413(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        response = env.client.put(
            f"/artifact/{artifact_id.hex}", content=PAYLOAD + b"extra"
        )

        assert response.status_code == 413
        assert rows(env.store, "SELECT COUNT(*) FROM artifact_blobs") == [(1,)]
        assert env.client.get(f"/artifact/{artifact_id.hex}").content == PAYLOAD

    def test_upload_exactly_at_declared_size_succeeds(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 200
        assert rows(env.store, "SELECT COUNT(*) FROM artifact_blobs") == [(1,)]


class TestArtifactCeiling:
    def test_undeclared_upload_over_ceiling_is_413(self, tmp_path):
        env = _make_env(tmp_path, max_artifact_bytes=1024)
        artifact_id = uuid.uuid4()

        response = env.client.put(
            f"/artifact/{artifact_id.hex}", content=_chunks(b"x" * 2048)
        )

        assert response.status_code == 413
        error = response.json()
        assert error["error"] == "payload_too_large"
        assert error["detail"] == "artifact body exceeds the 1024-byte limit"
        assert sorted(item.name for item in env.root.iterdir()) == ["tmp"]
        assert list((env.root / "tmp").iterdir()) == []
        assert rows(env.store, "SELECT COUNT(*) FROM artifact_blobs") == [(0,)]

    def test_declared_upload_exactly_at_ceiling_is_stored(self, tmp_path):
        env = _make_env(tmp_path, max_artifact_bytes=len(PAYLOAD))
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 200
        assert rows(env.store, "SELECT COUNT(*) FROM artifact_blobs") == [(1,)]
        assert env.client.get(f"/artifact/{artifact_id.hex}").content == PAYLOAD

    def test_undeclared_upload_exactly_at_ceiling_awaits_declaration(self, tmp_path):
        env = _make_env(tmp_path, max_artifact_bytes=len(PAYLOAD))
        artifact_id = uuid.uuid4()

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 202
        assert response.json() == {
            "status": "awaiting_declaration",
            "sha256": SHA_PAYLOAD,
            "size_bytes": len(PAYLOAD),
        }

    def test_ceiling_tighter_than_declaration_wins(self, tmp_path):
        env = _make_env(tmp_path, max_artifact_bytes=8)
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 413
        assert response.json()["detail"] == "artifact body exceeds the 8-byte limit"
        assert list((env.root / "tmp").iterdir()) == []
        assert rows(env.store, "SELECT COUNT(*) FROM artifact_blobs") == [(0,)]
