import hashlib
import os
import uuid as uuid_lib
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jernerics_schema import IngestError, IngestRequest, IngestResponse
from pydantic import BaseModel
from starlette.responses import FileResponse

from .ingest import IngestService, IngestServiceError
from .store import Store

_READ_ONLY_KEYWORDS = {"SELECT", "WITH", "VALUES", "EXPLAIN", "SHOW", "DESCRIBE"}
MAX_ROWS = 10_000
MAX_INGEST_BYTES = 8 * 1024 * 1024


class QueryRequest(BaseModel):
    sql: str
    params: list | None = None


def _is_read_only(sql: str) -> bool:
    first_word = sql.strip().split()[0].upper()
    return first_word in _READ_ONLY_KEYWORDS


def _make_auth_dependency(api_key: str):
    def check_bearer(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    return check_bearer


def _canonical_artifact_id(value: str) -> str:
    """Normalize a URL artifact id (32-hex or dashed) to the stored form."""
    try:
        return str(uuid_lib.UUID(value))
    except ValueError:
        raise HTTPException(
            status_code=400, detail="artifact id must be a UUID"
        ) from None


def _digest_mismatches(
    sha256: str, size: int, expected: tuple[str | None, int]
) -> bool:
    """True when a streamed blob disagrees with established facts.

    A ``None`` expected sha256 (declaration without a hash) adopts the
    blob's hash as truth; the size must always match.
    """
    expected_sha, expected_size = expected
    if expected_sha is not None and sha256 != expected_sha:
        return True
    return size != expected_size


class _IngestBodyLimit:
    """Rejects oversized /ingest bodies (413) before any parsing happens."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/ingest"
        ):
            for name, value in scope.get("headers", []):
                if name == b"content-length" and int(value) > MAX_INGEST_BYTES:
                    body = IngestError(
                        error="payload_too_large",
                        detail=(
                            f"request body exceeds the {MAX_INGEST_BYTES}-byte"
                            " ingest limit"
                        ),
                    ).model_dump(mode="json")
                    await JSONResponse(status_code=413, content=body)(
                        scope, receive, send
                    )
                    return
        await self.app(scope, receive, send)


def create_app(
    store: Store,
    *,
    api_key: str | None = None,
    artifacts_root: str | Path | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(_IngestBodyLimit)
    deps = [Depends(_make_auth_dependency(api_key))] if api_key else []
    ingest_service = IngestService(
        store,
        artifacts_root=Path(artifacts_root) if artifacts_root is not None else None,
    )

    @app.post("/query", response_model=None, dependencies=deps)
    def query(req: QueryRequest) -> JSONResponse:
        if not _is_read_only(req.sql):
            return JSONResponse(
                status_code=400,
                content={"error": "Only SELECT queries are allowed"},
            )
        try:
            columns, rows = store.query(req.sql, req.params)
        except Exception as e:
            return JSONResponse(
                status_code=400,
                content={"error": str(e)},
            )
        if len(rows) > MAX_ROWS:
            return JSONResponse(
                status_code=400,
                content={"error": f"Result exceeds maximum of {MAX_ROWS} rows"},
            )
        return JSONResponse(
            content={"columns": columns, "rows": [list(r) for r in rows]}
        )

    @app.post("/ingest", response_model=None, dependencies=deps)
    def ingest(req: IngestRequest) -> JSONResponse:
        try:
            result = ingest_service.apply(req)
        except IngestServiceError as e:
            return JSONResponse(
                status_code=409,
                content=IngestError(
                    error=e.error_code,
                    event_index=e.event_index,
                    event_id=e.event_id,
                    detail=e.detail,
                ).model_dump(mode="json"),
            )
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
        return JSONResponse(
            content=IngestResponse(
                accepted=result.applied,
                duplicates=result.duplicates,
                conflicts=result.conflicts,
            ).model_dump(mode="json")
        )

    if artifacts_root is not None:

        @app.put("/artifact/{artifact_id}", response_model=None, dependencies=deps)
        async def put_artifact(artifact_id: str, request: Request) -> JSONResponse:
            canonical = _canonical_artifact_id(artifact_id)
            root = Path(artifacts_root)
            tmp_dir = root / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            final = root / canonical[:2] / canonical
            rel_path = f"{canonical[:2]}/{canonical}"
            tmp = tmp_dir / uuid_lib.uuid4().hex
            digest = hashlib.sha256()
            size = 0
            try:
                # Blob streaming writes to local disk are this route's purpose;
                # blocking writes are acceptable on the single-node server.
                with open(tmp, "wb") as out:  # noqa: ASYNC230
                    async for chunk in request.stream():
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                sha256 = digest.hexdigest()

                declaration = store.artifact_declaration(canonical)
                if declaration is not None:
                    # A received blob is the truth once written (a None
                    # declared sha adopts the first blob's hash); otherwise
                    # verify against the declaration.
                    blob = store.artifact_blob(canonical)
                    if blob is not None:
                        expected = (blob[1], blob[2])
                    else:
                        expected = (declaration[2], declaration[3])
                    if _digest_mismatches(sha256, size, expected):
                        return JSONResponse(
                            status_code=409,
                            content={
                                "error": "conflict",
                                "detail": (
                                    f"artifact {canonical} already holds "
                                    f"sha256 {expected[0]} / size {expected[1]}"
                                ),
                            },
                        )
                    if blob is None:
                        final.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(tmp, final)
                        store.record_artifact_blob(canonical, rel_path, sha256, size)
                    return JSONResponse(
                        content={
                            "artifact_id": canonical,
                            "sha256": sha256,
                            "size_bytes": size,
                        }
                    )

                if final.exists():
                    existing_digest = hashlib.sha256()
                    existing_size = 0
                    with open(final, "rb") as f:  # noqa: ASYNC230
                        for chunk in iter(lambda: f.read(1024 * 1024), b""):
                            existing_digest.update(chunk)
                            existing_size += len(chunk)
                    if existing_digest.hexdigest() != sha256 or existing_size != size:
                        return JSONResponse(
                            status_code=409,
                            content={
                                "error": "conflict",
                                "detail": (
                                    f"artifact {canonical} already holds "
                                    "different bytes"
                                ),
                            },
                        )
                    return JSONResponse(
                        content={
                            "artifact_id": canonical,
                            "sha256": sha256,
                            "size_bytes": size,
                        }
                    )
                final.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp, final)
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "awaiting_declaration",
                        "sha256": sha256,
                        "size_bytes": size,
                    },
                )
            finally:
                tmp.unlink(missing_ok=True)

        @app.get("/artifact/{artifact_id}", response_model=None, dependencies=deps)
        def get_artifact(artifact_id: str) -> FileResponse:
            canonical = _canonical_artifact_id(artifact_id)
            declaration = store.artifact_declaration(canonical)
            if declaration is None:
                raise HTTPException(status_code=404, detail="unknown artifact")
            filename, content_type, _, _, _ = declaration
            blob = store.artifact_blob(canonical)
            if blob is None:
                raise HTTPException(status_code=404, detail="blob not received")
            rel_path, sha256, _ = blob
            path = Path(artifacts_root) / rel_path
            if not path.is_file():
                raise HTTPException(status_code=404, detail="blob not received")
            return FileResponse(
                path,
                media_type=content_type,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{filename.replace(chr(34), "")}"'
                    ),
                    "ETag": f'"{sha256}"',
                    "Cache-Control": "private, max-age=31536000, immutable",
                },
            )

    @app.get("/api/health", response_model=None, dependencies=deps)
    def health() -> JSONResponse:
        return JSONResponse(content={"ok": True})

    return app
