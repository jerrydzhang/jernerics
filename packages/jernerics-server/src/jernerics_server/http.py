import mimetypes
from collections.abc import Callable
from io import BytesIO

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .store import Store

_READ_ONLY_KEYWORDS = {"SELECT", "WITH", "VALUES", "EXPLAIN", "SHOW", "DESCRIBE"}
MAX_ROWS = 10_000


class QueryRequest(BaseModel):
    sql: str


def _is_read_only(sql: str) -> bool:
    first_word = sql.strip().split()[0].upper()
    return first_word in _READ_ONLY_KEYWORDS


def _make_auth_dependency(api_key: str):
    def check_bearer(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {api_key}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    return check_bearer


def _file_chunks(body, chunk_size: int = 65_536):
    while True:
        chunk = body.read(chunk_size)
        if not chunk:
            break
        yield chunk


def create_app(
    store: Store,
    *,
    api_key: str | None = None,
    s3_fetch: Callable[[str, str], tuple[BytesIO, str]] | None = None,
) -> FastAPI:
    app = FastAPI()
    deps = [Depends(_make_auth_dependency(api_key))] if api_key else []

    @app.post("/query", response_model=None, dependencies=deps)
    def query(req: QueryRequest) -> JSONResponse:
        if not _is_read_only(req.sql):
            return JSONResponse(
                status_code=400,
                content={"error": "Only SELECT queries are allowed"},
            )
        try:
            columns, rows = store.query(req.sql)
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

    @app.get("/api/sweeps", response_model=None, dependencies=deps)
    def list_sweeps() -> JSONResponse:
        sweeps = store.list_sweeps()
        return JSONResponse(content=sweeps)

    if s3_fetch is not None:

        @app.get(
            "/artifact/{project}/{study}/{trial_id}/{key}",
            response_model=None,
            dependencies=deps,
        )
        def artifact(project: str, study: str, trial_id: int, key: str) -> Response:
            _, rows = store.query(
                "SELECT filename FROM artifacts WHERE "
                "project = ? AND study_name = ? AND trial_id = ? AND key = ?",
                [project, study, trial_id, key],
            )
            if not rows:
                raise HTTPException(status_code=404, detail="Artifact not found")

            filename = rows[0][0]
            s3_key = f"{project}/{study}/{trial_id}/{key}"
            try:
                body, _ = s3_fetch("", s3_key)
            except FileNotFoundError:
                raise HTTPException(
                    status_code=404, detail="Artifact file not found"
                ) from None
            content_type = (
                mimetypes.guess_type(filename)[0] or "application/octet-stream"
            )
            return StreamingResponse(_file_chunks(body), media_type=content_type)

    return app
