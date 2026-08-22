import hashlib
import os
import uuid as uuid_lib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jernerics_schema import (
    ArtifactsQuery,
    ExecutionsQuery,
    IngestError,
    IngestRequest,
    IngestResponse,
    LineageQuery,
    ProjectsQuery,
    ProvenanceQuery,
    QueryErrorBody,
    QueryErrorResponse,
    SweepsQuery,
    TrialParamsQuery,
    TrialsQuery,
    ValueCatalogQuery,
    ValuesQuery,
)
from pydantic import BaseModel
from starlette.responses import FileResponse

from .ingest import IngestService, IngestServiceError
from .queries import QueryService, QueryServiceError
from .store import QueryResourceLimitError, Store

_READ_ONLY_KEYWORDS = {"SELECT", "WITH", "VALUES", "EXPLAIN", "SHOW", "DESCRIBE"}
MAX_ROWS = 10_000
MAX_INGEST_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_BYTES = 2 * 1024**3


def _is_read_only(sql: str) -> bool:
    first_word = sql.strip().split()[0].upper()
    return first_word in _READ_ONLY_KEYWORDS


def _records_response(
    records: Sequence[BaseModel | str], next_token: str | None = None
) -> JSONResponse:
    dumped = [
        record.model_dump(mode="json") if isinstance(record, BaseModel) else record
        for record in records
    ]
    return JSONResponse(content={"records": dumped, "next_token": next_token})


def _structured_error(code: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content=QueryErrorResponse(
            error=QueryErrorBody(code=code, detail=detail)
        ).model_dump(mode="json"),
    )


def _query_error(e: QueryServiceError) -> JSONResponse:
    return _structured_error(e.code, str(e))


class QueryRequest(BaseModel):
    sql: str
    params: list | None = None


_EMPTY_PROJECTS_QUERY = ProjectsQuery()


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


async def _reject_ingest_too_large(scope: dict, receive: Any, send: Any) -> None:
    body = IngestError(
        error="payload_too_large",
        detail=f"request body exceeds the {MAX_INGEST_BYTES}-byte ingest limit",
    ).model_dump(mode="json")
    await JSONResponse(status_code=413, content=body)(scope, receive, send)


class _IngestBodyLimit:
    """Rejects oversized /ingest bodies (413) before any parsing happens.

    Content-Length is checked up front when present; the streamed bytes are
    metered when it is not, so a chunked body cannot bypass the cap.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if not (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and scope.get("path") == "/ingest"
        ):
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", []):
            if name == b"content-length" and int(value) > MAX_INGEST_BYTES:
                await _reject_ingest_too_large(scope, receive, send)
                return
        messages: list[dict] = []
        total = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.disconnect":
                break
            if message["type"] != "http.request":
                continue
            total += len(message.get("body", b""))
            if total > MAX_INGEST_BYTES:
                await _reject_ingest_too_large(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

        async def replay() -> dict:
            return messages.pop(0) if messages else await receive()

        await self.app(scope, replay, send)


def create_app(
    store: Store,
    *,
    api_key: str | None = None,
    artifacts_root: str | Path | None = None,
    heartbeat_stale_s: float = 900.0,
    dashboard: bool = False,
    max_artifact_bytes: int | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(_IngestBodyLimit)
    deps = [Depends(_make_auth_dependency(api_key))] if api_key else []
    ingest_service = IngestService(
        store,
        artifacts_root=Path(artifacts_root) if artifacts_root is not None else None,
    )
    queries = QueryService(
        store,
        heartbeat_stale_s=heartbeat_stale_s,
        artifacts_root=Path(artifacts_root) if artifacts_root is not None else None,
    )
    dashboard_ctx = None
    artifact_get_deps: list = []
    if dashboard:
        from .dashboard import (
            build_dashboard_context,
            mount_dashboard,
            session_or_bearer_auth,
        )

        dashboard_ctx = build_dashboard_context(store, queries=queries, api_key=api_key)
        if api_key:
            # Same-origin artifact downloads accept the dashboard session;
            # uploads and every other endpoint stay bearer-only.
            artifact_get_deps = [Depends(session_or_bearer_auth(dashboard_ctx))]

    @app.post("/query", response_model=None, dependencies=deps)
    def query(req: QueryRequest) -> JSONResponse:
        if not _is_read_only(req.sql):
            return JSONResponse(
                status_code=400,
                content={"error": "Only SELECT queries are allowed"},
            )
        try:
            columns, rows = store.query(req.sql, req.params)
        except QueryResourceLimitError as e:
            return _structured_error("query_resource_limit", str(e))
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

    @app.post("/projects", response_model=None, dependencies=deps)
    def projects(req: ProjectsQuery = _EMPTY_PROJECTS_QUERY) -> JSONResponse:
        return _records_response(queries.projects())

    @app.post("/sweeps", response_model=None, dependencies=deps)
    def sweeps(req: SweepsQuery) -> JSONResponse:
        try:
            records, next_token = queries.sweeps(
                req.selection,
                states=req.states,
                page=req.page,
                page_token=req.page_token,
            )
        except QueryServiceError as e:
            return _query_error(e)
        return _records_response(records, next_token)

    @app.post("/trials", response_model=None, dependencies=deps)
    def trials(req: TrialsQuery) -> JSONResponse:
        try:
            records, next_token = queries.trials(
                req.selection,
                states=req.states,
                retry_roots_only=req.retry_roots_only,
                page=req.page,
                page_token=req.page_token,
            )
        except QueryServiceError as e:
            return _query_error(e)
        return _records_response(records, next_token)

    @app.post("/trial-params", response_model=None, dependencies=deps)
    def trial_params(req: TrialParamsQuery) -> JSONResponse:
        try:
            records, next_token = queries.trial_params(
                req.selection,
                kinds=req.kinds,
                page=req.page,
                page_token=req.page_token,
            )
        except QueryServiceError as e:
            return _query_error(e)
        return _records_response(records, next_token)

    @app.post("/lineage", response_model=None, dependencies=deps)
    def lineage(req: LineageQuery) -> JSONResponse:
        return _records_response(queries.lineage(req.selection))

    @app.post("/executions", response_model=None, dependencies=deps)
    def executions(req: ExecutionsQuery) -> JSONResponse:
        return _records_response(
            queries.executions(
                req.selection,
                states=req.states,
                derive=req.derive,
                heartbeat_stale_s=req.heartbeat_stale_s,
            )
        )

    @app.post("/value-catalog", response_model=None, dependencies=deps)
    def value_catalog(req: ValueCatalogQuery) -> JSONResponse:
        return _records_response(queries.value_catalog(req.selection))

    @app.post("/values", response_model=None, dependencies=deps)
    def values(req: ValuesQuery) -> JSONResponse:
        keys = tuple(req.keys or ())
        if req.key is not None:
            keys = (req.key, *keys)
        try:
            records, next_token = queries.values(
                req.selection,
                keys=keys or None,
                steps=req.steps,
                since_ns=req.since_ns,
                json_only=req.json_only,
                page=req.page,
                page_token=req.page_token,
            )
        except QueryServiceError as e:
            return _query_error(e)
        return _records_response(records, next_token)

    @app.post("/artifacts", response_model=None, dependencies=deps)
    def artifacts(req: ArtifactsQuery) -> JSONResponse:
        try:
            records, next_token = queries.artifacts(
                req.selection,
                keys=req.keys,
                received=req.received,
                source=req.source,
                page=req.page,
                page_token=req.page_token,
            )
        except QueryServiceError as e:
            return _query_error(e)
        return _records_response(records, next_token)

    @app.post("/provenance", response_model=None, dependencies=deps)
    def provenance(req: ProvenanceQuery) -> JSONResponse:
        return _records_response(queries.provenance(req.selection))

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
        artifact_max = (
            MAX_ARTIFACT_BYTES if max_artifact_bytes is None else max_artifact_bytes
        )

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
            declaration = store.artifact_declaration(canonical)
            blob = store.artifact_blob(canonical)
            if blob is not None:
                bound: int | None = blob[2]
            elif declaration is not None:
                bound = declaration[3]
            else:
                bound = None
            limit = min(artifact_max, bound) if bound is not None else artifact_max
            try:
                # Blob streaming writes to local disk are this route's purpose;
                # blocking writes are acceptable on the single-node server.
                with open(tmp, "wb") as out:  # noqa: ASYNC230
                    async for chunk in request.stream():
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if size > limit:
                            # Bytes past the stored/declared size or the
                            # artifact ceiling cannot match anyway: fail fast
                            # with 413 instead of streaming fully to a 409.
                            # The blob_uploader client already treats any
                            # non-2xx/non-409 as a manifest-stopping failure
                            # retried next sweep, so this is compatible.
                            return JSONResponse(
                                status_code=413,
                                content={
                                    "error": "payload_too_large",
                                    "detail": (
                                        f"artifact body exceeds the {limit}-byte limit"
                                    ),
                                },
                            )
                sha256 = digest.hexdigest()

                if declaration is not None:
                    # A received blob is the truth once written (a None
                    # declared sha adopts the first blob's hash); otherwise
                    # verify against the declaration.
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

        @app.get(
            "/dashboard/artifact/{artifact_id}",
            response_model=None,
            dependencies=artifact_get_deps or deps,
        )
        @app.get(
            "/artifact/{artifact_id}",
            response_model=None,
            dependencies=artifact_get_deps or deps,
        )
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

    if dashboard_ctx is not None:
        mount_dashboard(app, dashboard_ctx)

    return app
