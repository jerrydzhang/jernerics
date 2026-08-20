from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from jernerics_schema import IngestError, IngestRequest, IngestResponse
from pydantic import BaseModel

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
    ingest_service = IngestService(store)

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

    @app.get("/api/health", response_model=None, dependencies=deps)
    def health() -> JSONResponse:
        return JSONResponse(content={"ok": True})

    return app
