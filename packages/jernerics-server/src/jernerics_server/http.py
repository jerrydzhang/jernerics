import mimetypes
import statistics
from collections.abc import Callable
from io import BytesIO
from typing import TypedDict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from .store import Store

_READ_ONLY_KEYWORDS = {"SELECT", "WITH", "VALUES", "EXPLAIN", "SHOW", "DESCRIBE"}
MAX_ROWS = 10_000


class TrialResponse(TypedDict):
    trial_id: int
    status: str
    params: dict[str, float | int | str | bool]
    final_metrics: dict[str, float]
    artifact_keys: list[str]


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


def _compute_metric_stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"min": 0.0, "median": 0.0, "max": 0.0}
    return {
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
    }


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
    def list_sweeps(project: str | None = None) -> JSONResponse:
        sweeps = store.list_sweeps(project=project)
        return JSONResponse(content=sweeps)

    @app.get("/api/health", response_model=None, dependencies=deps)
    def health() -> JSONResponse:
        return JSONResponse(content={"ok": True})

    @app.get("/api/metrics", response_model=None, dependencies=deps)
    def get_metrics(project: str, study_name: str, key: str) -> JSONResponse:
        metrics = store.get_metrics_by_key(project, study_name, key)
        return JSONResponse(content=metrics)

    @app.get("/api/trials", response_model=None, dependencies=deps)
    def list_trials(
        project: str,
        study_name: str,
        metric_keys: str | None = None,
        limit: int | None = None,
    ) -> JSONResponse:
        if limit is not None and limit < 0:
            return JSONResponse(
                status_code=400,
                content={"error": "limit must be non-negative"},
            )
        trials = store.list_trials(project, study_name, limit=limit)
        if metric_keys:
            keys = [k.strip() for k in metric_keys.split(",") if k.strip()]
            for trial in trials:
                filtered_metrics = {
                    k: v for k, v in trial.get("final_metrics", {}).items() if k in keys
                }
                trial["final_metrics"] = filtered_metrics
        return JSONResponse(content=trials)

    @app.get("/api/artifacts", response_model=None, dependencies=deps)
    def list_artifacts(
        project: str, study_name: str, trial_id: int | None = None
    ) -> JSONResponse:
        artifacts = store.list_artifacts(project, study_name, trial_id)
        return JSONResponse(content=artifacts)

    @app.get("/api/results", response_model=None, dependencies=deps)
    def list_results(
        project: str,
        study_name: str,
        trial_id: int | None = None,
        key: str | None = None,
    ) -> JSONResponse:
        results = store.list_results(project, study_name, trial_id, key)
        return JSONResponse(content=results)

    @app.get("/api/params", response_model=None, dependencies=deps)
    def list_params(
        project: str,
        study_name: str,
        trial_id: int | None = None,
        key: str | None = None,
    ) -> JSONResponse:
        params = store.list_params(project, study_name, trial_id, key)
        return JSONResponse(content=params)

    @app.get("/api/sweep-summary", response_model=None, dependencies=deps)
    def sweep_summary(project: str, study_name: str) -> JSONResponse:
        summary = store.get_study_summary(project, study_name)
        if summary is None:
            raise HTTPException(
                status_code=404, detail=f"Study '{study_name}' not found"
            )
        return JSONResponse(
            content={
                "project": project,
                "study_name": study_name,
                "trial_count": summary["trial_count"],
                "completed_count": summary["completed_count"],
                "param_keys": summary["param_keys"],
                "final_metric_keys": summary["final_metric_keys"],
                "artifact_keys": summary["artifact_keys"],
            }
        )

    @app.get("/api/compare-sweeps", response_model=None, dependencies=deps)
    def compare_sweeps(
        project: str, left: str, right: str, metrics: str | None = None
    ) -> JSONResponse:
        left_summary = store.get_study_summary(project, left)
        right_summary = store.get_study_summary(project, right)

        if left_summary is None:
            raise HTTPException(status_code=404, detail=f"Study '{left}' not found")
        if right_summary is None:
            raise HTTPException(status_code=404, detail=f"Study '{right}' not found")

        left_param_keys_set = set(left_summary["param_keys"])
        right_param_keys_set = set(right_summary["param_keys"])
        left_metric_keys_set = set(left_summary["final_metric_keys"])
        right_metric_keys_set = set(right_summary["final_metric_keys"])
        left_artifact_keys_set = set(left_summary["artifact_keys"])
        right_artifact_keys_set = set(right_summary["artifact_keys"])

        param_keys = {
            "shared": sorted(left_param_keys_set & right_param_keys_set),
            "left_only": sorted(left_param_keys_set - right_param_keys_set),
            "right_only": sorted(right_param_keys_set - left_param_keys_set),
        }

        final_metric_keys = {
            "shared": sorted(left_metric_keys_set & right_metric_keys_set),
            "left_only": sorted(left_metric_keys_set - right_metric_keys_set),
            "right_only": sorted(right_metric_keys_set - left_metric_keys_set),
        }

        artifact_keys = {
            "shared": sorted(left_artifact_keys_set & right_artifact_keys_set),
            "left_only": sorted(left_artifact_keys_set - right_artifact_keys_set),
            "right_only": sorted(right_artifact_keys_set - left_artifact_keys_set),
        }

        metric_stats: dict[str, dict[str, dict[str, float]]] = {}
        shared_metrics = store.get_shared_final_metrics(project, left, right)
        for key, values in shared_metrics.items():
            metric_stats[key] = {
                "left": _compute_metric_stats(values["left"]),
                "right": _compute_metric_stats(values["right"]),
            }

        if metrics is not None:
            requested_keys = [k.strip() for k in metrics.split(",") if k.strip()]
            metric_stats = {
                k: v for k, v in metric_stats.items() if k in requested_keys
            }

        return JSONResponse(
            content={
                "left": left,
                "right": right,
                "left_trial_count": left_summary["trial_count"],
                "left_completed_count": left_summary["completed_count"],
                "right_trial_count": right_summary["trial_count"],
                "right_completed_count": right_summary["completed_count"],
                "param_keys": param_keys,
                "final_metric_keys": final_metric_keys,
                "artifact_keys": artifact_keys,
                "final_metric_stats": metric_stats,
            }
        )

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
