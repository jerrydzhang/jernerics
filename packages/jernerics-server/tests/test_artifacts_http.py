import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ArtifactSource,
    ExecutionStartEvent,
    IngestRequest,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
)
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.store import Store

T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
PAYLOAD = b"0123456789abcdef"
SHA_PAYLOAD = hashlib.sha256(PAYLOAD).hexdigest()


def at(offset_s: float) -> datetime:
    return T0 + timedelta(seconds=offset_s)


def rows(store: Store, sql: str, params: list | None = None) -> list[tuple]:
    _, data = store.query(sql, params or [])
    return data


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "artifacts.sqlite")
    root = tmp_path / "blobs"
    app = create_app(store, artifacts_root=root)
    return SimpleNamespace(store=store, root=root, client=TestClient(app))


def _declare(
    env,
    artifact_id: uuid.UUID,
    *,
    sha256: str | None = SHA_PAYLOAD,
    size_bytes: int = len(PAYLOAD),
    key: str = "model",
    source: ArtifactSource = "user",
) -> tuple[uuid.UUID, uuid.UUID]:
    """Ingest a minimal graph plus one artifact declaration."""
    sweep, trial, execution = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=at(0),
            project="proj",
            sweep_id=sweep,
            name=f"alpha-{sweep.hex[:8]}",
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
            key=key,
            filename="model.bin",
            content_type="application/octet-stream",
            size_bytes=size_bytes,
            sha256=sha256,
            source=source,
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
    return trial, execution


class TestDeclarationThenUpload:
    def test_upload_after_declaration_records_receipt_and_serves_bytes(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 200
        body = response.json()
        assert body["artifact_id"] == str(artifact_id)
        assert body["sha256"] == SHA_PAYLOAD
        assert body["size_bytes"] == len(PAYLOAD)
        assert rows(
            env.store,
            "SELECT rel_path, sha256, size_bytes FROM artifact_blobs",
        ) == [(f"{str(artifact_id)[:2]}/{artifact_id}", SHA_PAYLOAD, len(PAYLOAD))]
        assert rows(env.store, "SELECT received_ns IS NOT NULL FROM artifacts") == [
            (True,)
        ]

        served = env.client.get(f"/artifact/{artifact_id.hex}")
        assert served.status_code == 200
        assert served.content == PAYLOAD
        assert served.headers["content-type"].startswith("application/octet-stream")
        assert (
            served.headers["content-disposition"] == 'attachment; filename="model.bin"'
        )
        assert served.headers["etag"] == f'"{SHA_PAYLOAD}"'
        assert served.headers["cache-control"] == (
            "private, max-age=31536000, immutable"
        )

    def test_dashed_artifact_id_is_accepted(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        assert (
            env.client.put(f"/artifact/{artifact_id}", content=PAYLOAD).status_code
            == 200
        )
        assert env.client.get(f"/artifact/{artifact_id}").content == PAYLOAD

    def test_invalid_artifact_id_rejected(self, env):
        assert env.client.put("/artifact/nope", content=b"x").status_code == 400
        assert env.client.get("/artifact/nope").status_code == 400


class TestUploadBeforeDeclaration:
    def test_awaiting_declaration_then_materializer_joins_blob(self, env):
        artifact_id = uuid.uuid4()

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 202
        assert response.json() == {
            "status": "awaiting_declaration",
            "sha256": SHA_PAYLOAD,
            "size_bytes": len(PAYLOAD),
        }
        assert rows(env.store, "SELECT count(*) FROM artifact_blobs") == [(0,)]

        _declare(env, artifact_id)

        assert rows(env.store, "SELECT count(*) FROM artifact_blobs") == [(1,)]
        assert rows(env.store, "SELECT received_ns IS NOT NULL FROM artifacts") == [
            (True,)
        ]
        assert env.client.get(f"/artifact/{artifact_id.hex}").content == PAYLOAD

    def test_declaration_without_sha_adopts_blob_hash(self, env):
        artifact_id = uuid.uuid4()
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        _declare(env, artifact_id, sha256=None)

        assert rows(env.store, "SELECT sha256 FROM artifact_blobs") == [(SHA_PAYLOAD,)]


class TestReuploadSemantics:
    def test_identical_reupload_is_idempotent(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 200
        assert rows(env.store, "SELECT count(*) FROM artifact_blobs") == [(1,)]

    def test_different_bytes_conflict_and_original_survives(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        response = env.client.put(
            f"/artifact/{artifact_id.hex}", content=b"X" * len(PAYLOAD)
        )

        assert response.status_code == 409
        assert rows(env.store, "SELECT count(*) FROM artifact_blobs") == [(1,)]
        assert env.client.get(f"/artifact/{artifact_id.hex}").content == PAYLOAD

    def test_undeclared_reupload_same_bytes_idempotent(self, env):
        artifact_id = uuid.uuid4()
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        assert response.status_code == 200
        assert not list((env.root / "tmp").iterdir())

    def test_undeclared_reupload_different_bytes_conflicts(self, env):
        artifact_id = uuid.uuid4()
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        response = env.client.put(
            f"/artifact/{artifact_id.hex}", content=b"other-bytes"
        )

        assert response.status_code == 409


class TestPartialUploadCleanup:
    def test_short_body_leaves_no_temp_and_no_receipt(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id, size_bytes=100)

        response = env.client.put(f"/artifact/{artifact_id.hex}", content=b"short")

        assert response.status_code == 409
        assert list((env.root / "tmp").iterdir()) == []
        assert rows(env.store, "SELECT count(*) FROM artifact_blobs") == [(0,)]
        assert rows(env.store, "SELECT received_ns IS NULL FROM artifacts") == [(True,)]
        missing = env.client.get(f"/artifact/{artifact_id.hex}")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "blob not received"

    def test_leftover_temp_files_are_never_served(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        stale = env.root / "tmp" / "crash-leftover"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale")

        response = env.client.get(f"/artifact/{artifact_id.hex}")

        assert response.status_code == 404
        assert stale.read_bytes() == b"stale"


class TestRangeDownload:
    def test_range_request_returns_206_slice(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        response = env.client.get(
            f"/artifact/{artifact_id.hex}", headers={"Range": "bytes=0-3"}
        )

        assert response.status_code == 206
        assert response.content == PAYLOAD[:4]
        assert response.headers["content-range"] == f"bytes 0-3/{len(PAYLOAD)}"

    def test_absent_range_returns_200_full(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        response = env.client.get(f"/artifact/{artifact_id.hex}")

        assert response.status_code == 200
        assert response.content == PAYLOAD


class TestDuplicateKeys:
    def test_same_key_yields_two_distinct_artifacts(self, env):
        first, second = uuid.uuid4(), uuid.uuid4()
        _declare(
            env,
            first,
            key="model",
            sha256=hashlib.sha256(b"v1").hexdigest(),
            size_bytes=2,
        )
        _declare(
            env,
            second,
            key="model",
            sha256=hashlib.sha256(b"v2").hexdigest(),
            size_bytes=2,
        )

        assert (
            env.client.put(f"/artifact/{first.hex}", content=b"v1").status_code == 200
        )
        assert (
            env.client.put(f"/artifact/{second.hex}", content=b"v2").status_code == 200
        )

        assert rows(env.store, "SELECT count(*) FROM artifacts WHERE key='model'") == [
            (2,)
        ]
        assert env.client.get(f"/artifact/{first.hex}").content == b"v1"
        assert env.client.get(f"/artifact/{second.hex}").content == b"v2"


class TestMissingBlob:
    def test_declared_but_never_uploaded_reports_not_received(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)

        response = env.client.get(f"/artifact/{artifact_id.hex}")

        assert response.status_code == 404
        assert response.json()["detail"] == "blob not received"
        availability = rows(
            env.store,
            "SELECT a.key, b.artifact_id IS NOT NULL AS received "
            "FROM artifacts a LEFT JOIN artifact_blobs b "
            "ON a.artifact_id = b.artifact_id",
        )
        assert availability == [("model", 0)]

    def test_unknown_artifact_is_a_distinct_404(self, env):
        response = env.client.get(f"/artifact/{uuid.uuid4().hex}")
        assert response.status_code == 404
        assert response.json()["detail"] == "unknown artifact"


class TestBlobDeclarationMismatch:
    def test_mismatch_records_conflict_without_rejecting_batch(self, env):
        artifact_id = uuid.uuid4()
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        other_sha = "b" * 64
        response = env.client.post(
            "/ingest",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "events": [
                    event.model_dump(mode="json")
                    for event in _declaration_events(
                        artifact_id, sha256=other_sha, size_bytes=len(PAYLOAD)
                    )
                ],
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] >= 1
        assert [conflict["kind"] for conflict in body["conflicts"]] == [
            "artifact_blob_mismatch"
        ]
        assert rows(env.store, "SELECT count(*) FROM artifact_blobs") == [(0,)]
        assert rows(env.store, "SELECT received_ns IS NULL FROM artifacts") == [(True,)]

    def test_service_reports_mismatch_conflict(self, tmp_path):
        store = Store(tmp_path / "svc.sqlite")
        root = tmp_path / "blobs"
        artifact_id = uuid.uuid4()
        final = root / str(artifact_id)[:2] / str(artifact_id)
        final.parent.mkdir(parents=True)
        final.write_bytes(PAYLOAD)

        service = IngestService(store, artifacts_root=root)
        result = service.apply(_declaration_request(artifact_id, sha256="c" * 64))

        assert result.conflicts[0].kind == "artifact_blob_mismatch"
        assert rows(store, "SELECT count(*) FROM artifact_blobs") == [(0,)]

    def test_matching_blob_joins_through_service(self, tmp_path):
        store = Store(tmp_path / "svc.sqlite")
        root = tmp_path / "blobs"
        artifact_id = uuid.uuid4()
        final = root / str(artifact_id)[:2] / str(artifact_id)
        final.parent.mkdir(parents=True)
        final.write_bytes(PAYLOAD)

        service = IngestService(store, artifacts_root=root)
        result = service.apply(_declaration_request(artifact_id))

        assert result.conflicts == ()
        assert rows(store, "SELECT count(*) FROM artifact_blobs") == [(1,)]
        assert rows(store, "SELECT received_ns IS NOT NULL FROM artifacts") == [(True,)]


class TestMetadataQueriesWithoutBlobBytes:
    def test_sql_metadata_works_with_blob_file_deleted(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id)
        env.client.put(f"/artifact/{artifact_id.hex}", content=PAYLOAD)

        blob_file = env.root / str(artifact_id)[:2] / str(artifact_id)
        blob_file.unlink()

        metadata = rows(
            env.store,
            "SELECT a.key, a.filename, b.sha256, b.size_bytes "
            "FROM artifacts a JOIN artifact_blobs b ON a.artifact_id = b.artifact_id",
        )
        assert metadata == [("model", "model.bin", SHA_PAYLOAD, len(PAYLOAD))]
        assert env.client.get(f"/artifact/{artifact_id.hex}").status_code == 404


class TestArtifactRouteAuth:
    def test_bearer_required_when_api_key_set(self, tmp_path):
        store = Store(tmp_path / "auth.sqlite")
        app = create_app(store, api_key="secret123", artifacts_root=tmp_path / "b")
        client = TestClient(app)
        artifact_id = uuid.uuid4().hex

        assert client.put(f"/artifact/{artifact_id}", content=b"x").status_code == 401
        assert client.get(f"/artifact/{artifact_id}").status_code == 401
        ok = client.put(
            f"/artifact/{artifact_id}",
            content=b"x",
            headers={"authorization": "Bearer secret123"},
        )
        assert ok.status_code == 202


def _declaration_events(
    artifact_id: uuid.UUID, *, sha256: str | None, size_bytes: int
) -> list:
    sweep, trial, execution = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    return [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=at(0),
            project="proj",
            sweep_id=sweep,
            name="alpha",
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
        ),
    ]


def _declaration_request(
    artifact_id: uuid.UUID, *, sha256: str | None = SHA_PAYLOAD
) -> IngestRequest:
    return IngestRequest(
        protocol_version=PROTOCOL_VERSION,
        events=_declaration_events(artifact_id, sha256=sha256, size_bytes=len(PAYLOAD)),
    )


class TestSchemaColumns:
    def test_artifacts_table_carries_context_and_source(self, env):
        artifact_id = uuid.uuid4()
        _declare(env, artifact_id, source="system")

        stored = rows(env.store, "SELECT context_json, source FROM artifacts")
        assert stored == [(None, "system")]
