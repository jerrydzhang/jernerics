from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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


def create_app(store: Store, *, api_key: str | None = None) -> FastAPI:
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

    @app.get("/api/health", response_model=None, dependencies=deps)
    def health() -> JSONResponse:
        return JSONResponse(content={"ok": True})

    return app
