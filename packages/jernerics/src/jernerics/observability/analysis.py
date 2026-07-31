"""Analysis primitives that query tracking data directly.

Each function takes a ``Queryable`` — anything exposing
``query(sql, params) -> (columns, rows)``. The server's :class:`Store`
satisfies this structurally (tests use it directly); the CLI wraps the
HTTP ``/query`` endpoint behind the same interface (see
``jernerics.observability.RemoteStore``).

Runs are identified by ``(study_name, trial_id)``. Every scoped function
takes ``project`` so a multi-project server is queried correctly.
"""

from typing import Any, Protocol

# Candidate priority metrics for the runs table, tried in order.
PRIORITY_METRICS = ("loss", "error", "accuracy", "r2")

# Slope windows are the first/last 10% of logged points. Below this many
# points in a window the slope is too noisy, so it is skipped.
_MIN_SLOPE_POINTS = 5


class Queryable(Protocol):
    def query(
        self, sql: str, params: list | None = None
    ) -> tuple[list[str], list[tuple]]: ...


def _run_label(study_name: str, trial_id: int) -> str:
    return study_name if trial_id == 0 else f"{study_name}:{trial_id}"


def _parse_run(spec: str) -> tuple[str, int]:
    if ":" in spec:
        name, _, tid = spec.partition(":")
        return name, int(tid)
    return spec, 0


def _reconstruct_param(
    float_val: float | None,
    int_val: int | None,
    string_val: str | None,
    bool_val: int | None,
) -> bool | int | float | str | None:
    if bool_val is not None:
        return bool(bool_val)
    if int_val is not None:
        return int(int_val)
    if float_val is not None:
        return float(float_val)
    if string_val is not None:
        return str(string_val)
    return None


def compute_slope(
    steps: list[int], values: list[float], window_start: int, window_end: int
) -> float:
    """Least-squares slope of ``values`` over ``steps`` within the index
    window ``[window_start, window_end)``.

    Returns 0.0 when the window holds fewer than two points or all steps
    are identical (no x-range to slope against).
    """
    xs = steps[window_start:window_end]
    ys = values[window_start:window_end]
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return 0.0
    return num / den


def compute_metric_analysis(
    store: Queryable,
    project: str,
    study_name: str,
    trial_id: int,
    key: str,
) -> dict[str, Any]:
    """First→last change plus early/recent slope for one metric series.

    ``first``/``last`` follow chronological (seq) order. Slopes use the
    step axis: only points with a non-null ``step`` are placed on it.
    Each slope is skipped (``None``) when its 10% window holds fewer
    than ``_MIN_SLOPE_POINTS`` points.
    """
    _, rows = store.query(
        "SELECT seq, step, scalar_val FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "AND key = ? AND value_type = 'scalar' AND scalar_val IS NOT NULL "
        "ORDER BY seq, step",
        [project, study_name, trial_id, key],
    )

    if not rows:
        return {
            "first": None,
            "last": None,
            "change": None,
            "early_slope": None,
            "recent_slope": None,
            "early_range": None,
            "recent_range": None,
            "n_points": 0,
        }

    values = [float(r[2]) for r in rows]
    first = values[0]
    last = values[-1]

    stepped = [(int(r[1]), float(r[2])) for r in rows if r[1] is not None]
    stepped.sort(key=lambda p: p[0])
    n = len(stepped)

    early_slope = None
    recent_slope = None
    early_range = None
    recent_range = None
    window = round(0.1 * n)
    if window >= _MIN_SLOPE_POINTS:
        steps = [p[0] for p in stepped]
        svals = [p[1] for p in stepped]
        early_slope = compute_slope(steps, svals, 0, window)
        early_range = [steps[0], steps[window - 1]]
        recent_slope = compute_slope(steps, svals, n - window, n)
        recent_range = [steps[n - window], steps[n - 1]]

    return {
        "first": first,
        "last": last,
        "change": last - first,
        "early_slope": early_slope,
        "recent_slope": recent_slope,
        "early_range": early_range,
        "recent_range": recent_range,
        "n_points": len(values),
    }


def _all_metric_keys(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> list[str]:
    _, rows = store.query(
        "SELECT DISTINCT key FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "AND value_type = 'scalar' AND scalar_val IS NOT NULL",
        [project, study_name, trial_id],
    )
    return sorted(r[0] for r in rows)


def _last_metric_value(
    store: Queryable, project: str, study_name: str, trial_id: int, key: str
) -> float | None:
    _, rows = store.query(
        "SELECT scalar_val FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "AND key = ? AND value_type = 'scalar' AND scalar_val IS NOT NULL "
        "ORDER BY step DESC, seq DESC LIMIT 1",
        [project, study_name, trial_id, key],
    )
    return float(rows[0][0]) if rows else None


def _run_status(store: Queryable, project: str, study_name: str, trial_id: int) -> str:
    _, rows = store.query(
        "SELECT 1 FROM trial_end WHERE project = ? AND study_name = ? "
        "AND trial_id = ? LIMIT 1",
        [project, study_name, trial_id],
    )
    return "completed" if rows else "running"


def _run_params(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> dict[str, Any]:
    _, rows = store.query(
        "SELECT key, float_val, int_val, string_val, bool_val FROM params "
        "WHERE project = ? AND study_name = ? AND trial_id = ?",
        [project, study_name, trial_id],
    )
    params: dict[str, Any] = {}
    for key, fv, iv, sv, bv in rows:
        params[key] = _reconstruct_param(fv, iv, sv, bv)
    return params


def _run_artifacts(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> list[str]:
    _, rows = store.query(
        "SELECT DISTINCT key FROM artifacts "
        "WHERE project = ? AND study_name = ? AND trial_id = ? ORDER BY key",
        [project, study_name, trial_id],
    )
    return [r[0] for r in rows]


def _run_step_bounds(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> tuple[int | None, int | None]:
    _, rows = store.query(
        "SELECT MIN(step), MAX(step) FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "AND value_type = 'scalar' AND step IS NOT NULL",
        [project, study_name, trial_id],
    )
    if not rows or rows[0][0] is None:
        return None, None
    return int(rows[0][0]), int(rows[0][1])


def _run_timestamp_bounds(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> tuple[int | None, int | None]:
    _, rows = store.query(
        "SELECT MIN(ts), MAX(ts) FROM ("
        "SELECT timestamp_ns AS ts FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "UNION ALL SELECT timestamp_ns FROM params "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "UNION ALL SELECT timestamp_ns FROM artifacts "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "UNION ALL SELECT timestamp_ns FROM trial_end "
        "WHERE project = ? AND study_name = ? AND trial_id = ?)",
        [project, study_name, trial_id] * 4,
    )
    if not rows or rows[0][0] is None:
        return None, None
    return int(rows[0][0]), int(rows[0][1])


def _priority_metric(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> tuple[str | None, float | None]:
    keys = set(_all_metric_keys(store, project, study_name, trial_id))
    for candidate in PRIORITY_METRICS:
        if candidate in keys:
            return candidate, _last_metric_value(
                store, project, study_name, trial_id, candidate
            )
    return None, None


def get_all_runs(store: Queryable, project: str) -> list[dict[str, Any]]:
    """One summary dict per ``(study_name, trial_id)`` known to the store."""
    _, rows = store.query(
        "SELECT DISTINCT study_name, trial_id FROM ("
        "SELECT study_name, trial_id FROM tracked_values WHERE project = ? "
        "UNION SELECT study_name, trial_id FROM params WHERE project = ? "
        "UNION SELECT study_name, trial_id FROM artifacts WHERE project = ?) "
        "ORDER BY study_name, trial_id",
        [project, project, project],
    )

    runs: list[dict[str, Any]] = []
    for study_name, trial_id in rows:
        trial_id = int(trial_id)
        min_step, max_step = _run_step_bounds(store, project, study_name, trial_id)
        created_ns, ended_ns = _run_timestamp_bounds(
            store, project, study_name, trial_id
        )
        duration_s = (ended_ns - created_ns) / 1e9 if created_ns and ended_ns else None
        priority_key, priority_value = _priority_metric(
            store, project, study_name, trial_id
        )
        runs.append(
            {
                "study_name": study_name,
                "trial_id": trial_id,
                "label": _run_label(study_name, trial_id),
                "status": _run_status(store, project, study_name, trial_id),
                "min_step": min_step,
                "max_step": max_step,
                "duration_s": duration_s,
                "created_ns": created_ns,
                "params": _run_params(store, project, study_name, trial_id),
                "priority_key": priority_key,
                "priority_value": priority_value,
            }
        )
    return runs


def get_run_summary(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> dict[str, Any]:
    """Full detail for one run: params, per-metric analysis, artifacts."""
    min_step, max_step = _run_step_bounds(store, project, study_name, trial_id)
    created_ns, ended_ns = _run_timestamp_bounds(store, project, study_name, trial_id)
    metrics = {
        key: compute_metric_analysis(store, project, study_name, trial_id, key)
        for key in _all_metric_keys(store, project, study_name, trial_id)
    }
    return {
        "study_name": study_name,
        "trial_id": trial_id,
        "label": _run_label(study_name, trial_id),
        "status": _run_status(store, project, study_name, trial_id),
        "min_step": min_step,
        "max_step": max_step,
        "duration_s": (
            (ended_ns - created_ns) / 1e9 if created_ns and ended_ns else None
        ),
        "params": _run_params(store, project, study_name, trial_id),
        "metrics": metrics,
        "artifacts": _run_artifacts(store, project, study_name, trial_id),
    }


def get_run_diff(
    store: Queryable,
    project: str,
    run_a: str,
    run_b: str,
) -> dict[str, Any]:
    """Compare two runs: differing/matching params and final metric values."""
    a_name, a_trial = _parse_run(run_a)
    b_name, b_trial = _parse_run(run_b)

    a_params = _run_params(store, project, a_name, a_trial)
    b_params = _run_params(store, project, b_name, b_trial)
    a_status = _run_status(store, project, a_name, a_trial)
    b_status = _run_status(store, project, b_name, b_trial)
    _, a_max_step = _run_step_bounds(store, project, a_name, a_trial)
    _, b_max_step = _run_step_bounds(store, project, b_name, b_trial)

    all_param_keys = sorted(set(a_params) | set(b_params))
    param_diff: list[dict[str, Any]] = []
    matched: list[str] = []
    for key in all_param_keys:
        av = a_params.get(key)
        bv = b_params.get(key)
        if av != bv:
            param_diff.append({"key": key, "a": av, "b": bv})
        else:
            matched.append(key)

    a_metric_keys = set(_all_metric_keys(store, project, a_name, a_trial))
    b_metric_keys = set(_all_metric_keys(store, project, b_name, b_trial))
    metric_diff: list[dict[str, Any]] = []
    for key in sorted(a_metric_keys | b_metric_keys):
        av = (
            _last_metric_value(store, project, a_name, a_trial, key)
            if key in a_metric_keys
            else None
        )
        bv = (
            _last_metric_value(store, project, b_name, b_trial, key)
            if key in b_metric_keys
            else None
        )
        change = None
        if av is not None and bv is not None:
            change = bv - av
        metric_diff.append({"key": key, "a": av, "b": bv, "change": change})

    return {
        "run_a": {
            "label": _run_label(a_name, a_trial),
            "study_name": a_name,
            "trial_id": a_trial,
            "status": a_status,
            "max_step": a_max_step,
        },
        "run_b": {
            "label": _run_label(b_name, b_trial),
            "study_name": b_name,
            "trial_id": b_trial,
            "status": b_status,
            "max_step": b_max_step,
        },
        "param_diff": param_diff,
        "param_match_count": len(matched),
        "param_match": matched,
        "metric_diff": metric_diff,
    }


def get_metric_series(
    store: Queryable, project: str, study_name: str, trial_id: int, key: str
) -> dict[str, Any] | None:
    """Raw step/value series for one metric, regardless of value_type.

    Returns ``None`` when the key does not exist for the run. Otherwise
    returns a dict with ``value_type`` ('scalar' or 'json') and ``series``
    — a list ordered by ``seq`` of ``{step, value, seq, timestamp_ns}``.

    For scalar metrics ``value`` is a float; for text/json metrics it is
    a string.
    """
    _, rows = store.query(
        "SELECT seq, step, value_type, scalar_val, text_val, timestamp_ns "
        "FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "AND key = ? ORDER BY seq",
        [project, study_name, trial_id, key],
    )
    if not rows:
        return None

    value_type = rows[0][2]
    series: list[dict[str, Any]] = []
    for seq, step, _vtype, scalar_val, text_val, ts in rows:
        value = float(scalar_val) if value_type == "scalar" else text_val
        series.append(
            {
                "step": int(step) if step is not None else None,
                "value": value,
                "seq": int(seq),
                "timestamp_ns": int(ts),
            }
        )
    return {"value_type": value_type, "series": series}


def get_metric_keys(
    store: Queryable, project: str, study_name: str, trial_id: int
) -> list[dict[str, Any]]:
    """All metric keys for a run with value_type and point counts."""
    _, rows = store.query(
        "SELECT key, value_type, COUNT(*) FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ? "
        "GROUP BY key, value_type ORDER BY key",
        [project, study_name, trial_id],
    )
    return [
        {"key": r[0], "value_type": r[1], "count": int(r[2])} for r in rows
    ]


def run_exists(store: Queryable, project: str, study_name: str, trial_id: int) -> bool:
    """True if the run has any tracked data (value, param, artifact, or
    completion marker)."""
    _, rows = store.query(
        "SELECT 1 WHERE "
        "EXISTS (SELECT 1 FROM tracked_values "
        "WHERE project = ? AND study_name = ? AND trial_id = ?) "
        "OR EXISTS (SELECT 1 FROM params "
        "WHERE project = ? AND study_name = ? AND trial_id = ?) "
        "OR EXISTS (SELECT 1 FROM artifacts "
        "WHERE project = ? AND study_name = ? AND trial_id = ?) "
        "OR EXISTS (SELECT 1 FROM trial_end "
        "WHERE project = ? AND study_name = ? AND trial_id = ?) LIMIT 1",
        [project, study_name, trial_id] * 4,
    )
    return bool(rows)
