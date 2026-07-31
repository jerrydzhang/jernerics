import asyncio
import mimetypes
from pathlib import Path
from typing import IO

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .store import Store

_READ_ONLY_KEYWORDS = {"SELECT", "WITH", "VALUES", "EXPLAIN", "SHOW", "DESCRIBE"}
MAX_ROWS = 10_000


def _open_binary(path: Path) -> IO[bytes]:
    return open(path, "wb")


def _write_chunk(f: IO[bytes], chunk: bytes) -> None:
    f.write(chunk)


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


def create_app(
    store: Store,
    *,
    api_key: str | None = None,
    artifacts_root: str | Path | None = None,
) -> FastAPI:
    app = FastAPI()
    deps = [Depends(_make_auth_dependency(api_key))] if api_key else []
    artifacts_dir = Path(artifacts_root) if artifacts_root else None

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
    def ingest(envelope: dict) -> JSONResponse:
        store.insert_event(envelope)
        return JSONResponse(content={"ok": True})

    if artifacts_dir is not None:

        @app.post(
            "/artifact/{project}/{study}/{trial_id}/{key}",
            response_model=None,
            dependencies=deps,
        )
        async def upload_artifact(
            request: Request,
            project: str,
            study: str,
            trial_id: int,
            key: str,
        ) -> JSONResponse:
            target = artifacts_dir / project / study / str(trial_id) / key
            target.parent.mkdir(parents=True, exist_ok=True)
            f = await asyncio.to_thread(_open_binary, target)
            try:
                async for chunk in request.stream():
                    if chunk:
                        await asyncio.to_thread(_write_chunk, f, chunk)
            finally:
                await asyncio.to_thread(f.close)
            return JSONResponse(content={"ok": True})

        @app.get(
            "/artifact/{project}/{study}/{trial_id}/{key}",
            response_model=None,
            dependencies=deps,
        )
        def download_artifact(
            project: str, study: str, trial_id: int, key: str
        ) -> StreamingResponse:
            path = artifacts_dir / project / study / str(trial_id) / key
            if not path.exists():
                raise HTTPException(status_code=404, detail="Artifact not found")

            _, rows = store.query(
                "SELECT filename FROM artifacts WHERE "
                "project = ? AND study_name = ? AND trial_id = ? AND key = ?",
                [project, study, trial_id, key],
            )
            filename = rows[0][0] if rows else key
            media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

            def iterate():
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        yield chunk

            return StreamingResponse(
                iterate(),
                media_type=media_type,
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )

    @app.get("/api/health", response_model=None, dependencies=deps)
    def health() -> JSONResponse:
        return JSONResponse(content={"ok": True})

    return app
