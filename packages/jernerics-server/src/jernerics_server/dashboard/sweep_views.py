import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, quote

from dash import dcc, html
from dash.development.base_component import Component
from dash_ag_grid import AgGrid
from jernerics_schema import Selection, encode_selection
from plotly import graph_objects as go

from . import analysis, components, figures
from .components import MISSING, Empty
from .page import head_cell, limit_row, scroll_table, status_dot
from .render import SortColumn, sortable_columns
from .routes import ROUTES_BASE, parse_route
from .service import DashboardService

SWEEP_VIEWS = ("series", "points", "search", "optuna")

_GRID_DEFAULTS: dict[str, Any] = {
    "sortable": True,
    "filter": True,
    "resizable": True,
    "minWidth": 100,
}

_STATE_DOTS = {
    "completed": "completed",
    "fail": "failed",
}

_PYTHON_BASE_URL = "http://localhost:8000"


def view_from_search(search: str | None) -> str | None:
    """The active ``?view=`` sub-view of a sweep URL; unknown names mean
    the overview."""
    values = parse_qs((search or "").lstrip("?")).get("view")
    value = values[0] if values else None
    return value if value in SWEEP_VIEWS else None


def sweep_href(project: str, sweep_id: str, view: str | None = None) -> str:
    """The sweep page URL, optionally showing one sub-view."""
    url = f"{ROUTES_BASE}/project/{quote(project, safe='')}/sweep/{sweep_id}"
    return f"{url}?view={view}" if view else url


def route_sweep(pathname: str | None) -> tuple[str, str] | None:
    """(project, sweep_id) when the route is a sweep page, else ``None``."""
    spec = parse_route(pathname)
    if spec.kind != "sweep":
        return None
    return spec.object_id or "", spec.sub_id or ""


def sweep_tray(sweep_id: str) -> dict[str, Any]:
    """The analysis scope narrowed to exactly this sweep."""
    return {"sweeps": [sweep_id]}


def facts_digest(service: DashboardService, sweep_id: str) -> str:
    """Cheap digest of one sweep's stored facts — the sub-views' poll
    guard, mirroring the sweep page's tick gate."""
    payload = json.dumps(service.sweep_facts(sweep_id), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def python_disclosure(project: str, sweep_id: str) -> html.Details:
    """The sweep's effective Selection as an opaque token plus the
    runnable handoff snippet, disclosed in place."""
    selection = Selection(project=project, sweeps=(uuid.UUID(sweep_id),))
    token = encode_selection(selection)
    snippet = analysis.python_snippet(token, project, _PYTHON_BASE_URL)
    style = {"whiteSpace": "pre-wrap", "overflowX": "auto"}
    return html.Details(
        [
            html.Summary("Open in Python"),
            html.Div(
                [
                    html.Span("Copy the token or the snippet:", className="annotate"),
                    dcc.Clipboard(content=token, title="Copy selection token"),
                    dcc.Clipboard(content=snippet, title="Copy runnable snippet"),
                ],
                className="actions",
            ),
            html.Section(
                html.Pre(token, className="config-json", style=style),
                className="section",
            ),
            html.Section(
                html.Pre(snippet, className="config-json", style=style),
                className="section",
            ),
            html.P(
                "The token decodes to the exact effective membership via "
                "jernerics_schema.decode_selection; point TrackingClient at "
                "your server.",
                className="annotate",
            ),
        ],
        className="python-disclosure",
    )


# -- Series ----------------------------------------------------------------


def default_series_state(keys: Sequence[str] = ()) -> dict[str, Any]:
    """Series sub-view state: active metrics, each block's display and
    axis-scale choice, and the digest pair the poll gate reads."""
    return {
        "keys": list(keys),
        "display": {},
        "scale": {},
        "cheap": "",
        "digests": {},
    }


def series_snapshot_fetch(
    service: DashboardService,
    project: str,
    sweep_id: str,
    keys: list[str],
    now_ns: int,
) -> dict[str, Any]:
    """The canonical series snapshot over this sweep's scope."""
    doc = analysis.edited_view(
        None,
        {"series": {**analysis.default_view_state()["series"], "keys": list(keys)}},
    )
    return analysis.series_snapshot(service, project, sweep_tray(sweep_id), doc, now_ns)


def _key_options(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    return list(snapshot.get("key_options") or [])


def series_key_digests(
    service: DashboardService, project: str, sweep_id: str
) -> dict[str, str]:
    """Cheap per-key digest: point/trial volume identity per scalar
    series key — the poll gate that refreshes only the keys that
    gained observations (jernerics-1r00)."""
    return {
        entry["key"]: hashlib.sha256(
            json.dumps(
                [entry["points"], entry["trials"], entry["extent"]], default=str
            ).encode()
        ).hexdigest()[:16]
        for entry in service.analysis_value_keys(project, sweep_tray(sweep_id))
        if entry["kind"] == "scalar" and entry["steps"]
    }


def _key_payload(
    series: list[dict[str, Any]], numbers: dict[str, int]
) -> dict[str, Any]:
    """One key's store payload: exact stats from the full series, then
    the series thinned to the figure point cap."""
    return {
        "series": [
            {**entry, "points": figures.downsample_points(entry["points"])}
            for entry in series
        ],
        "stats": [
            [number, trial, last, low, high, points, step]
            for number, trial, last, low, high, points, step in _trial_stats(
                series, numbers
            )
        ],
    }


def series_split(
    snapshot: dict[str, Any], keys: list[str]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """(per-key store payloads, global facts) from one full snapshot —
    the split the view ships so poll refreshes move only changed keys."""
    numbers = {row["trial_id"]: row["number"] for row in snapshot.get("trials") or []}
    payloads = {
        key: _key_payload(
            list((snapshot.get("per_key") or {}).get(key, {}).get("series", [])),
            numbers,
        )
        for key in keys
    }
    snap = {
        name: snapshot.get(name)
        for name in ("trials", "varying", "key_options", "reduction", "fingerprint")
    }
    return payloads, snap


def series_keys_refetch(
    service: DashboardService,
    project: str,
    sweep_id: str,
    keys: list[str],
    trials: list[dict[str, Any]],
    now_ns: int,
) -> dict[str, dict[str, Any]]:
    """Fresh payloads for ONLY ``keys`` — the digest-gated incremental
    refresh; every other key's stored payload stays untouched."""
    numbers = {row["trial_id"]: row["number"] for row in trials}
    merged = analysis.merge_series_keys(
        service,
        project,
        sweep_tray(sweep_id),
        {"reduction": "none", "trials": trials, "per_key": {}},
        keys,
        now_ns,
    )
    return {
        key: _key_payload(
            list((merged.get("per_key") or {}).get(key, {}).get("series", [])),
            numbers,
        )
        for key in keys
    }


def series_chip_spans(state: dict[str, Any]) -> list[Any]:
    """The active-metric chips: bare spans — the chips container and
    add-picker ids live on the page-mounted chrome only, so callback
    refreshes can never duplicate them (jernerics-hjip)."""
    return [
        html.Span(
            [
                key,
                html.A("×", href="#", id={"sweep-series-drop": key}),
            ],
            className="chip",
        )
        for key in state["keys"]
    ]


def series_chips(state: dict[str, Any], snapshot: dict[str, Any]) -> list[Any]:
    """Server-rendered chips container plus the add-metric picker."""
    offered = {entry["value"] for entry in _key_options(snapshot)}
    return [
        html.Div(series_chip_spans(state), id="sweep-series-chips", className="chips"),
        dcc.Dropdown(
            id="sweep-series-add",
            placeholder="＋ add metric…",
            options=[
                {"label": key, "value": key}
                for key in sorted(offered - set(state["keys"]))
            ],
            value=None,
            clearable=False,
            searchable=True,
            className="addkey",
        ),
    ]


def series_key_figure(
    key: str, series: list[dict[str, Any]], state: dict[str, Any]
) -> go.Figure:
    axis = analysis.default_axis_state()
    axis["scale"] = state["scale"].get(key) or "linear"
    display = state["display"].get(key) or "median_iqr"
    return figures.stacked_figure(
        [{"key": key, "series": series}],
        {key: axis},
        display="all" if display == "all" else "median_iqr",
    )


def _trial_stats(
    series: list[dict[str, Any]], numbers: dict[str, int]
) -> list[tuple[int, str, float, float, float, int, int]]:
    """Per-trial (number, trial_id, last, min, max, points, last step),
    executions folded into one row."""
    folded: dict[str, list[tuple[int, float]]] = {}
    for entry in series:
        folded.setdefault(entry["trial"], []).extend(entry["points"])
    stats = []
    for trial, points in folded.items():
        values = [value for _, value in points]
        last_step, last = max(points, key=lambda point: point[0])
        stats.append(
            (
                numbers.get(trial, -1),
                trial,
                last,
                min(values),
                max(values),
                len(points),
                last_step,
            )
        )
    return sorted(stats)


def _stats_rows(key: str, rows: list[list[Any]]):
    formatted: list[Component | str] = []
    for number, trial, last, low, high, points, step in rows:
        label = f"#{number}" if number >= 0 else components.short_id(trial)
        formatted.append(
            html.Tr(
                [
                    html.Td(label, className="num"),
                    html.Td(analysis.param_text(last), className="num"),
                    html.Td(analysis.param_text(low), className="num"),
                    html.Td(analysis.param_text(high), className="num"),
                    html.Td(str(points), className="num"),
                    html.Td(str(step), className="num"),
                ],
                className="trial-row",
                id={"sweep-series-row": f"{key}:{trial}"},
                n_clicks=0,
            )
        )
    return formatted


def series_key_head(key: str, series: list[dict[str, Any]]) -> list[Any]:
    """The block header children: metric name and distinct-trial count."""
    return [
        html.H2(key),
        html.Span(
            f"· {len({entry['trial'] for entry in series})} trials",
            className="annotate",
        ),
    ]


def series_key_stats(key: str, stats: list[list[Any]]) -> Any:
    """The per-trial stats table for one key from its stored rows."""
    return scroll_table(
        [
            head_cell("Trial", numeric=True),
            head_cell("Last", numeric=True),
            head_cell("Min", numeric=True),
            head_cell("Max", numeric=True),
            head_cell("Points", numeric=True),
            head_cell("Last step", numeric=True),
        ],
        _stats_rows(key, stats),
    )


def series_blocks(
    state: dict[str, Any], payloads: dict[str, dict[str, Any]]
) -> list[Any]:
    """One block per active metric: display and scale toggles, the
    figure, per-trial stats, and the key's payload store. Every key
    mounts the same components — even with no observations — so the
    pattern-matched outputs stay aligned with the tree."""
    blocks: list[Any] = []
    for key in state["keys"]:
        payload = payloads.get(key) or {"series": [], "stats": []}
        blocks.append(
            html.Div(
                [
                    html.Div(
                        series_key_head(key, payload["series"]),
                        className="plot-head",
                        id={"sweep-series-head": key},
                    ),
                    html.Div(
                        [
                            dcc.RadioItems(
                                id={"sweep-series-display": key},
                                options=[
                                    {"label": " Median + IQR", "value": "median_iqr"},
                                    {"label": " All raw", "value": "all"},
                                ],
                                value=state["display"].get(key) or "median_iqr",
                                inline=True,
                            ),
                            dcc.RadioItems(
                                id={"sweep-series-scale": key},
                                options=[
                                    {"label": " Linear", "value": "linear"},
                                    {"label": " Log", "value": "log"},
                                ],
                                value=state["scale"].get(key) or "linear",
                                inline=True,
                            ),
                        ],
                        className="plot-controls",
                    ),
                    dcc.Graph(
                        id={"sweep-series-fig": key},
                        figure=series_key_figure(key, payload["series"], state),
                        clear_on_unhover=True,
                    ),
                    html.Div(
                        series_key_stats(key, payload["stats"]),
                        id={"sweep-series-stats": key},
                    ),
                    dcc.Store(id={"sweep-series-key": key}, data=payload),
                ],
                className="keyblock",
                id={"sweep-series-block": key},
            )
        )
    if not blocks:
        blocks = [Empty("No metric selected — add one above.")]
    return blocks


def _finals_from_series(
    payloads: dict[str, dict[str, Any]], keys: list[str]
) -> dict[str, dict[str, Any]]:
    """Each trial's last logged value per active key — the pcp's final
    dimensions. Thinning keeps the last point, so finals stay exact."""
    finals: dict[str, dict[str, Any]] = {}
    for key in keys:
        for entry in (payloads.get(key) or {}).get("series", []):
            per_trial = finals.setdefault(entry["trial"], {})
            for step, value in entry["points"]:
                current = per_trial.get(key)
                if current is None or step >= current[0]:
                    per_trial[key] = (step, value)
    return {
        trial: {key: value for key, (_, value) in per_trial.items()}
        for trial, per_trial in finals.items()
    }


def _dim(label: str, values: list[Any]) -> dict[str, Any]:
    vals = [
        float(value)
        if isinstance(value, int | float) and not isinstance(value, bool)
        else math.nan
        for value in values
    ]
    present = [value for value in vals if not math.isnan(value)]
    return {
        "label": label,
        "values": vals,
        "range": figures.padded_range(present),
    }


def series_pcp_outputs(
    state: dict[str, Any],
    payloads: dict[str, dict[str, Any]],
    trials: list[dict[str, Any]],
) -> tuple[Any, dict[str, list[str]]]:
    """(figure, pcp-data) for the Params → final-values plot: one line
    per trial whose varying params and active finals are all numeric,
    in row order."""
    ordered = sorted(trials, key=lambda row: (row["sweep_id"], row["number"]))
    numeric = set(figures.numeric_param_keys(ordered))
    varying = [key for key in analysis.varying_param_keys(ordered) if key in numeric][
        : figures.MAX_PARAM_DIMS
    ]
    finals = _finals_from_series(payloads, state["keys"])
    columns: list[tuple[str, list[Any]]] = [
        (
            key,
            [(trial.get("params") or {}).get(key) for trial in ordered],
        )
        for key in varying
    ]
    columns.extend(
        (
            f"{key} (final)",
            [finals.get(row["trial_id"], {}).get(key) for row in ordered],
        )
        for key in state["keys"]
    )
    return _pcp_figure(ordered, columns)


def _pcp_figure(
    ordered: list[dict[str, Any]], columns: list[tuple[str, list[Any]]]
) -> tuple[Any, dict[str, list[str]]]:
    """One parcoords line per trial complete across every column; each
    dimension's range covers every plotted value."""
    lines: list[list[float]] = []
    tks: list[str] = []
    for index, row in enumerate(ordered):
        vals = [_number(values[index]) for _, values in columns]
        if not vals or any(math.isnan(value) for value in vals):
            continue
        lines.append(vals)
        tks.append(row["trial_id"])
    if not columns:
        return Empty(
            "No varying numeric params and no selected metric — nothing to plot."
        ), {"tks": []}
    dims = [
        _dim(label, [line[position] for line in lines])
        for position, (label, _values) in enumerate(columns)
    ]
    return figures.points_parcoords(dims), {"tks": tks}


def _number(value: Any) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return math.nan


def series_body(
    service: DashboardService, project: str, sweep_id: str, now_ns: int
) -> html.Div:
    """The Series sub-view: metric chips, per-metric blocks, and the
    shared Params → final-values selection plot, all server-rendered
    once; callbacks own refresh and edits."""
    keys = [
        entry["key"]
        for entry in service.analysis_value_keys(project, sweep_tray(sweep_id))
        if entry["kind"] == "scalar" and entry["steps"]
    ]
    state = default_series_state(keys)
    state["digests"] = series_key_digests(service, project, sweep_id)
    snapshot = series_snapshot_fetch(service, project, sweep_id, keys, now_ns)
    payloads, snap = series_split(snapshot, keys)
    figure, pcp_data = series_pcp_outputs(state, payloads, snap["trials"] or [])
    return html.Div(
        [
            html.Div(
                [
                    *series_chips(state, snapshot),
                    html.Span(
                        id="sweep-series-updated",
                        className="annotate",
                        children=analysis.updated_ago(now_ns),
                    ),
                ],
                className="series-controls",
            ),
            html.Div(series_blocks(state, payloads), id="sweep-series-blocks"),
            html.H2("Params → final values"),
            html.Div(
                [
                    html.Span(id="sweep-series-pcp-note", className="num"),
                    html.Button(
                        "Clear selection",
                        id="sweep-series-clear",
                        n_clicks=0,
                        style={"display": "none"},
                    ),
                ],
                className="sel-note-row",
            ),
            dcc.Graph(
                id="sweep-series-pcp",
                figure=figure,
                clear_on_unhover=True,
            ),
            dcc.Store(id="sweep-series-state", data=state),
            dcc.Store(id="sweep-series-snap", data=snap),
            dcc.Store(id="sweep-series-pcp-data", data=pcp_data),
            dcc.Store(id="sweep-series-sel", data={"tks": []}),
            dcc.Store(id="sweep-series-echo"),
        ],
    )


# -- Points ----------------------------------------------------------------


def points_outcome(
    trials: list[dict[str, Any]], keys: list[str], finals: dict
) -> str | None:
    """The sweep's outcome key: the objective when any trial carries
    one, else the first scalar key with a numeric final value."""
    if any(trial.get("objective") is not None for trial in trials):
        return "objective"
    for key in keys:
        if any(
            isinstance(per_trial.get(key), int | float)
            and not isinstance(per_trial.get(key), bool)
            for per_trial in finals.values()
        ):
            return key
    return None


def points_view(
    service: DashboardService, project: str, sweep_id: str
) -> dict[str, Any]:
    """(view data, outcome, scalar keys) for the sweep's points grid,
    the trial objective synthesized into the finals so it can anchor
    the params → outcome plot."""
    trials = service.analysis_trials(project, sweep_tray(sweep_id))
    keys = analysis.points_scalar_keys(service, project, sweep_tray(sweep_id))
    finals = service.analysis_finals(project, sweep_tray(sweep_id))
    outcome = points_outcome(trials, keys, finals)
    if outcome == "objective":
        finals = {
            trial["trial_id"]: {
                **finals.get(trial["trial_id"], {}),
                "objective": trial.get("objective"),
            }
            for trial in trials
        }
    view = analysis.points_view_data(trials, keys, finals, outcome or "")
    return {"view": view, "outcome": outcome, "keys": keys, "trials": trials}


def points_columns(view: dict[str, Any]) -> list[SortColumn]:
    """Grid columns for one sweep: no sweep column — every row is this
    sweep's trial."""
    return [
        SortColumn("number", "Trial", "numeric"),
        SortColumn("state", "State", "string"),
        *[
            SortColumn(
                entry["key"],
                entry["key"],
                "numeric" if entry["numeric"] else "string",
                sort_field=f"{entry['key']}_raw" if entry["numeric"] else None,
                definition={"valueFormatter": {"function": "renderMissing(x)"}},
            )
            for entry in view["keys"]
        ],
    ]


def points_body(service: DashboardService, project: str, sweep_id: str) -> html.Div:
    """The Points sub-view: trials × final scalars grid with the
    params → outcome parallel coordinates and the selection split."""
    built = points_view(service, project, sweep_id)
    view = built["view"]
    if not view["rows"]:
        return html.Div(Empty("No trials under this sweep — nothing to compare yet."))
    plotted = built["outcome"] and len(view["dims"]) > 1 and view["with_outcome"]
    return html.Div(
        [
            html.Section(
                [
                    html.H2("Trials · final scalars"),
                    html.P(
                        "The last logged value of each scalar key is the "
                        "trial's final scalar. Click rows or brush the plot "
                        "to select; selected rows stay, the rest hide.",
                        className="hint",
                    ),
                    html.Div(
                        [
                            html.Span(id="sweep-points-note", className="num"),
                            html.Button(
                                "Clear selection",
                                id="sweep-points-clear",
                                n_clicks=0,
                                style={"display": "none"},
                            ),
                        ],
                        className="sel-note-row",
                    ),
                    AgGrid(
                        id="sweep-points-grid",
                        rowData=view["rows"],
                        columnDefs=sortable_columns(points_columns(view)),
                        defaultColDef=_GRID_DEFAULTS,
                        dashGridOptions=components.grid_options(
                            rowSelection={
                                "mode": "multiRow",
                                "checkboxes": True,
                                "headerCheckboxSelection": True,
                                "enableClickSelection": True,
                            },
                        ),
                        getRowId="params.data.tk",
                        className="ag-theme-alpine grid",
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H2(
                        f"Params → {built['outcome']} (final)"
                        if built["outcome"]
                        else "Params → outcome"
                    ),
                    *(
                        [
                            dcc.Graph(
                                id="sweep-points-figure",
                                figure=figures.points_parcoords(view["dims"]),
                            )
                        ]
                        if plotted
                        else [
                            Empty(
                                "No params → outcome plot: the outcome has "
                                "no numeric values under this sweep."
                            )
                        ]
                    ),
                ],
                className="section",
            ),
            dcc.Store(id="sweep-points-data", data={"tks": view["tks"]}),
            dcc.Store(id="sweep-points-sel", data={"tks": []}),
            dcc.Store(id="sweep-points-echo"),
        ],
    )


# -- Search ----------------------------------------------------------------


def search_data_fetch(
    service: DashboardService, project: str, sweep_id: str, now_ns: int
) -> dict[str, Any]:
    """The searchable trial facts for one sweep: numbers, states,
    objectives, and each trial's varying configuration text."""
    trials = service.analysis_trials(project, sweep_tray(sweep_id))
    varying = analysis.varying_param_keys(trials)
    return {
        "rows": [
            {
                "tk": row["trial_id"],
                "number": row["number"],
                "state": row.get("state") or MISSING,
                "objective": row.get("objective"),
                "config": analysis.trial_config_text(row, varying),
            }
            for row in sorted(trials, key=lambda row: (row["sweep_id"], row["number"]))
        ],
        "cheap": facts_digest(service, sweep_id),
        "at_ns": now_ns,
    }


def search_rows(data: dict[str, Any], needle: str) -> list[html.Tr]:
    """The trial rows matching the filter text: trial number, state,
    config, and objective text all match case-folded substrings."""
    rows: list[html.Tr] = []
    for row in data.get("rows") or []:
        objective = MISSING if row["objective"] is None else f"{row['objective']:.4g}"
        haystack = " ".join(
            (
                f"#{row['number']}",
                str(row["state"]),
                row["config"],
                str(row["objective"] if row["objective"] is not None else ""),
            )
        ).casefold()
        if needle and needle not in haystack:
            continue
        rows.append(
            html.Tr(
                [
                    html.Td(f"#{row['number']}", className="num"),
                    html.Td(
                        status_dot(_STATE_DOTS.get(row["state"], "running")),
                    ),
                    html.Td(objective, className="num"),
                    html.Td(row["config"] or MISSING),
                ]
            )
        )
    return rows


def search_body(
    service: DashboardService, project: str, sweep_id: str, now_ns: int
) -> html.Div:
    """The Search sub-view: a debounced filter over this sweep's
    trials — number, state, config, and objective text."""
    data = search_data_fetch(service, project, sweep_id, now_ns)
    rows = search_rows(data, "")
    return html.Div(
        [
            limit_row(
                dcc.Input(
                    id="sweep-search-q",
                    type="search",
                    placeholder="Filter trials…",
                    debounce=True,
                ),
                html.Span(
                    f"{len(rows)} of {len(data['rows'])} trials",
                    id="sweep-search-note",
                    className="annotate",
                ),
            ),
            html.Div(
                scroll_table(
                    [
                        head_cell("Trial", numeric=True),
                        head_cell("State"),
                        head_cell("Objective", numeric=True),
                        head_cell("Config"),
                    ],
                    rows,
                    sortable=True,
                ),
                id="sweep-search-results",
            ),
            dcc.Store(id="sweep-search-data", data=data),
        ],
    )


# -- Optuna ----------------------------------------------------------------


def numeric_param_keys_for(trials: list[dict[str, Any]]) -> list[str]:
    """Numeric param keys under the sweep, for the contour axis pickers."""
    return figures.numeric_param_keys(trials)


def optuna_axes(trials: list[dict[str, Any]]) -> dict[str, str | None]:
    """Default contour axes: the first two numeric param keys."""
    keys = numeric_param_keys_for(trials)
    return {"x": keys[0] if keys else None, "y": keys[1] if len(keys) > 1 else None}


def optuna_body(
    service: DashboardService, project: str, sweep_id: str, now_ns: int
) -> html.Div:
    """The Optuna sub-view: the study-style figures over the sweep's
    mirrored optimizer state — objective history, params → objective
    parallel coordinates, parameter slices, the 2-D contour, and the
    trial timeline."""
    trials = service.analysis_trials(project, sweep_tray(sweep_id))
    axes = optuna_axes(trials)
    keys = numeric_param_keys_for(trials)
    options = [{"label": key, "value": key} for key in keys]
    return html.Div(
        [
            html.Section(
                [
                    html.H2("Objective history"),
                    dcc.Graph(
                        id="sweep-optuna-history",
                        figure=figures.optimization_history(trials),
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H2("Params → objective"),
                    dcc.Graph(
                        id="sweep-optuna-parcoords",
                        figure=figures.parallel_coordinates(trials),
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H2("Parameter slices"),
                    dcc.Graph(
                        id="sweep-optuna-slices",
                        figure=figures.slice_figure(trials),
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H2("Objective contour"),
                    html.Div(
                        [
                            dcc.Dropdown(
                                id="sweep-optuna-x",
                                options=options,
                                value=axes["x"],
                                clearable=False,
                                placeholder="x param…",
                            ),
                            dcc.Dropdown(
                                id="sweep-optuna-y",
                                options=options,
                                value=axes["y"],
                                clearable=False,
                                placeholder="y param…",
                            ),
                        ],
                        className="optuna-axes",
                    ),
                    dcc.Graph(
                        id="sweep-optuna-contour",
                        figure=contour_figure(trials, axes["x"], axes["y"]),
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H2("Trial timeline"),
                    dcc.Graph(
                        id="sweep-optuna-timeline",
                        figure=figures.trial_timeline(trials),
                    ),
                ],
                className="section",
            ),
            dcc.Store(
                id="sweep-optuna-data",
                data={"trials": trials, "cheap": facts_digest(service, sweep_id)},
            ),
        ],
    )


def contour_figure(
    trials: list[dict[str, Any]], x_key: str | None, y_key: str | None
) -> go.Figure:
    """The 2-D objective contour when both axes are chosen, else an
    honest placeholder."""
    if not x_key or not y_key or x_key == y_key:
        figure = go.Figure()
        figure.update_layout(title="contour needs two distinct numeric params")
        return figure
    return figures.contour_figure(trials, x_key, y_key)
