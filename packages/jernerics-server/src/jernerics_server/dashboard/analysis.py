"""Session view state for the dashboard's analysis surfaces.

One canonical view document holds what a session keeps across renders:
the investigation Series controls, the auto-refresh intent, the retry
families picked on sweep pages, and the highlighted trials. Every write
goes through :func:`edited_view`. All data flows through
DashboardService; the investigation Series view keeps its fetched
snapshot in a page store so view-only edits issue zero reads.
"""

import json
import math
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, quote

from dash import dcc, html, no_update
from dash_ag_grid import AgGrid

from . import components, figures
from .components import (
    MISSING,
    Empty,
    Error,
    relative_time,
)
from .render import SortColumn, sortable_columns
from .routes import ROUTES_BASE
from .service import ANALYSIS_REDUCTIONS, DashboardService

EMPTY_TRAY: dict[str, Any] = {
    "sweeps": [],
    "trials": [],
    "families": [],
    "executions": [],
    "expand": False,
}
"""Selection dimensions of an analysis tray: sweep ids, explicit trial
ids, picked retry-family roots, explicit execution ids, and the
per-family expansion toggle. The analysis reads resolve a tray to a
typed Selection; the view document's ``scope`` keeps only what a
session accumulates across pages (the picked families)."""

_GRID_DEFAULTS: dict[str, Any] = {
    "sortable": True,
    "filter": True,
    "resizable": True,
    "minWidth": 100,
}
INVESTIGATION_VIEWS = ("compare", "series", "points", "search")
"""The investigation page's view row."""


_SERIES_MODES = ("stacked", "overlay")
_TRIAL_DISPLAYS = ("all", "highlighted", "median_iqr")
_AXIS_SCALES = ("linear", "log")
_AXIS_RANGES = ("auto", "custom")


def default_axis_state() -> dict[str, Any]:
    """A per-key y-axis at its default: linear scale, auto range."""
    return {"scale": "linear", "range": "auto", "min": None, "max": None}


def default_scope_state() -> dict[str, Any]:
    """The view document's scope group: the session-picked families."""
    return {"families": []}


def default_view_state() -> dict[str, Any]:
    """The view document with every control at its default."""
    return {
        "auto_refresh": False,
        "scope": default_scope_state(),
        "highlighted_trials": [],
        "series": {
            "keys": [],
            "mode": "stacked",
            "reduction": "none",
            "trial_display": "all",
            "context_filters": {},
            "color": None,
            "facet": None,
            "axes": {},
            "overlay_axis": default_axis_state(),
        },
    }


def edited_view(
    current: dict[str, Any] | None, changes: dict[str, Any]
) -> dict[str, Any]:
    """The one door for view-doc writes: ``changes`` applied over
    ``current`` (defaults when absent)."""
    return {**(current or default_view_state()), **changes}


def loaded_option_values(options: Any) -> set[str] | None:
    """Value set of dropdown options as Dash reports them, flattening
    grouped options; ``None`` when the list has not loaded yet."""
    if not isinstance(options, list):
        return None
    values: set[str] = set()
    for option in options:
        if not isinstance(option, dict):
            continue
        if "options" in option:
            values.update(
                entry["value"]
                for entry in option["options"]
                if isinstance(entry, dict) and "value" in entry
            )
        elif "value" in option:
            values.add(option["value"])
    return values


def _gated_value(desired: str | None, loaded: set[str] | None) -> Any:
    """A dropdown value to write, or ``no_update`` when writing it would
    race the options: a dropdown drops a value its options do not carry
    and fires the loss back as an edit."""
    if desired is None:
        return None
    return desired if loaded is not None and desired in loaded else no_update


def _gated_keys(keys: list[str], loaded: set[str] | None) -> Any:
    """A multi-select value to write, or ``no_update`` when writing it
    would race the options: a dropdown drops values its options do not
    carry and fires the loss back as an edit."""
    if loaded is None:
        return no_update if keys else []
    if keys and not set(keys) <= loaded:
        return no_update
    return list(keys)


def control_values(
    doc: dict[str, Any] | None,
    loaded: dict[str, set[str] | None],
) -> tuple[Any, ...]:
    """(keys, mode, reduction, color, facet, trial_display, auto_refresh)
    the analysis controls take from the view state; dropdown values
    arrive only once their options carry them."""
    doc = doc or default_view_state()
    return (
        _gated_keys(doc["series"]["keys"], loaded.get("keys")),
        doc["series"]["mode"],
        doc["series"]["reduction"],
        _gated_value(doc["series"]["color"], loaded.get("color")),
        _gated_value(doc["series"]["facet"], loaded.get("facet")),
        doc["series"]["trial_display"],
        ["auto"] if doc["auto_refresh"] else [],
    )


def view_from_controls(
    current: dict[str, Any] | None,
    *,
    keys: list[str] | None,
    mode: str | None,
    reduction: str | None,
    color: str | None,
    facet: str | None,
    trial_display: str | None = None,
    auto_refresh: bool | None = None,
    edited: set[str],
) -> dict[str, Any]:
    """View state after a control edit. Only the fields named in
    ``edited`` (the controls the event actually carried) are
    authoritative; the rest survive from ``current`` — a control-sync
    write fires the edit callback with every input, and a control whose
    options have not loaded reports None, which must not read as a
    clear. Fields without controls at all (context filters, per-key
    axes, the overlay axis, highlighted families) always survive."""
    doc = current or default_view_state()
    series = dict(doc["series"])
    if "keys" in edited:
        series["keys"] = list(
            dict.fromkeys(key for key in keys or [] if isinstance(key, str) and key)
        )
    if "mode" in edited and mode in _SERIES_MODES:
        series["mode"] = mode
    if "reduction" in edited:
        series["reduction"] = reduction if reduction in ANALYSIS_REDUCTIONS else "none"
    if "trial_display" in edited:
        series["trial_display"] = (
            trial_display if trial_display in _TRIAL_DISPLAYS else "all"
        )
    if "color" in edited:
        series["color"] = color or None
    if "facet" in edited:
        series["facet"] = facet or None
    auto_refresh_state = bool(doc["auto_refresh"])
    if "auto_refresh" in edited and auto_refresh is not None:
        auto_refresh_state = bool(auto_refresh)
    return edited_view(
        doc,
        {
            "series": series,
            "auto_refresh": auto_refresh_state,
        },
    )


_CONTROL_IDS = {
    "analysis-key": "keys",
    "analysis-mode": "mode",
    "analysis-reduction": "reduction",
    "analysis-display": "trial_display",
    "analysis-auto-refresh": "auto_refresh",
    "analysis-color": "color",
    "analysis-facet": "facet",
}


def edited_fields(triggered: Any) -> set[str]:
    """View-doc fields named by the triggered callback inputs. Triggered
    prop ids may carry a plain string id or a resolved pattern id (the
    JSON of the dict plus ``.prop``); both map through the same table."""
    fields = set()
    for prop in triggered:
        control = str(prop).split(".", 1)[0]
        if control.startswith("{"):
            try:
                resolved = json.loads(control)
            except ValueError:
                continue
            control = (
                str(next(iter(resolved))) if isinstance(resolved, dict) else control
            )
        field = _CONTROL_IDS.get(control)
        if field:
            fields.add(field)
    return fields


def _counted(count: int, singular: str, plural: str) -> str:
    """``count`` with the noun form that matches it."""
    return f"{count} {singular if count == 1 else plural}"


def seed_sweeps_from_search(search: str | None) -> list[str]:
    """The editor route's ``?sweeps=`` CSV as sorted-unique sweep ids.
    Unknown parts are kept, not dropped — the preview names them."""
    values = parse_qs((search or "").lstrip("?")).get("sweeps")
    if not values:
        return []
    return sorted({part for part in values[0].split(",") if part})


_WORKSPACE_SEARCH_KEYS = {"view", "sel"}


def investigation_scope_state(
    members: Sequence[Any] | None, member: str | None
) -> tuple[dict[str, Any], str | None]:
    """(scope group, resolved member) for the analysis over an
    investigation's members; an unknown member falls back to all
    members. The scope sweeps are exactly the materialized membership."""
    picked = sorted({str(sweep) for sweep in members or ()})
    scoped = member if member in picked else None
    if scoped:
        picked = [scoped]
    return {**EMPTY_TRAY, "sweeps": picked}, scoped


def investigation_view_href(
    project: str,
    investigation_id: str,
    view: str,
    member: str | None = None,
) -> str:
    """Investigation page URL showing one view, optionally narrowed to
    one member sweep (the sweep hub's Series/Points links). Compare
    never carries a member scope; unknown views fall back to it."""
    active = view if view in INVESTIGATION_VIEWS else "compare"
    if active == "compare":
        member = None
    target = f"{ROUTES_BASE}/project/{project}/investigation/{investigation_id}"
    params = [f"view={active}"] if active != "compare" else []
    if member:
        params.append(f"member={quote(member, safe='')}")
    return f"{target}?{'&'.join(params)}" if params else target


def _coverage_option(entry: dict[str, Any]) -> dict[str, str]:
    """Picker option for one value key: key, points, and trials in the
    label; the full coverage facts in the option's title tooltip."""
    low, high = entry["extent"]
    extent = f"steps {low}-{high}" if entry["steps"] else "no steps beyond 0"
    trials = _counted(entry["trials"], "trial", "trials")
    return {
        "label": f"{entry['key']} · {entry['points']} pts · {trials}",
        "value": entry["key"],
        "title": (
            f"{entry['kind']} · {entry['points']} points · {trials} · "
            f"{_counted(entry['families'], 'family', 'families')} · {extent}"
        ),
    }


def scope_fingerprint(project: str | None, tray: dict[str, Any] | None) -> str:
    """Canonical identity of the (project, scope) a snapshot serves."""
    return json.dumps(
        {"project": project or "", "scope": tray or {}},
        sort_keys=True,
        separators=(",", ":"),
    )


def series_snapshot(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
    now_ns: int,
) -> dict[str, Any]:
    """The canonical ``analysis-series-data`` payload: scope fingerprint
    and reduction, unfiltered per-key enriched series, trial/config and
    varying-param facts, key options, context dimensions with every
    filter value, and scope liveness — everything presentation rebuilds
    from with zero further reads."""
    doc = view_doc or default_view_state()
    series_doc = doc["series"]
    keys = list(series_doc["keys"])
    if not project or service is None:
        return {
            "fingerprint": "",
            "reduction": series_doc["reduction"],
            "incomplete": False,
            "at_ns": now_ns,
            "per_key": {key: {"series": []} for key in keys},
            "trials": [],
            "varying": [],
            "key_options": [],
            "dims": [],
        }
    fingerprint = scope_fingerprint(project, tray)
    coverage = service.analysis_value_keys(project, tray)
    offered = {
        entry["key"]: _coverage_option(entry)
        for entry in coverage
        if entry["kind"] == "scalar" and entry["steps"]
    }
    per_key = service.analysis_series(
        project, tray, keys, series_doc["reduction"] or "none"
    )
    trials = service.analysis_trials(project, tray)
    _enrich_series(per_key, trials)
    return {
        "fingerprint": fingerprint,
        "reduction": series_doc["reduction"],
        "incomplete": service.analysis_scope_incomplete(project, tray),
        "at_ns": now_ns,
        "per_key": {entry["key"]: {"series": entry["series"]} for entry in per_key},
        "trials": trials,
        "varying": varying_param_keys(trials),
        "key_options": [
            offered.get(
                key,
                {
                    "label": f"{key} · absent under this scope",
                    "value": key,
                    "title": "picked, but not offered under the current scope",
                },
            )
            for key in sorted({*offered, *keys})
        ],
        "dims": service.analysis_context_catalog(project, tray),
    }


def snapshot_status(
    snapshot: dict[str, Any] | None,
    fingerprint: str,
    reduction: str,
    keys: Sequence[str],
) -> tuple[bool, list[str]]:
    """(usable, missing keys) for a snapshot against the current scope,
    reduction, and wanted keys; ``usable`` False means rebuild from
    scratch, otherwise only the missing keys need fetching."""
    if snapshot is None:
        return False, []
    if (
        snapshot.get("fingerprint") != fingerprint
        or snapshot.get("reduction") != reduction
    ):
        return False, []
    return True, [key for key in keys if key not in (snapshot.get("per_key") or {})]


def merge_series_keys(
    service: DashboardService,
    project: str,
    tray: dict[str, Any] | None,
    snapshot: dict[str, Any],
    added: Sequence[str],
    now_ns: int,
) -> dict[str, Any]:
    """Snapshot after fetching ONLY the added keys and merging their
    enriched series; every other fact is reused untouched."""
    per_key = service.analysis_series(
        project, tray, list(added), snapshot["reduction"] or "none"
    )
    _enrich_series(per_key, snapshot.get("trials") or [])
    return {
        **snapshot,
        "at_ns": now_ns,
        "per_key": {
            **snapshot["per_key"],
            **{entry["key"]: {"series": entry["series"]} for entry in per_key},
        },
    }


def _axis_controls(axis: dict[str, Any], pattern: Any, *, overlay: bool = False):
    """One compact control block: scale, range mode, custom bounds, and
    Reset — pattern ids keyed by metric for stacked panels, static ids
    for the one shared overlay axis."""
    ids = (
        {
            "scale": "analysis-overlay-scale",
            "range": "analysis-overlay-range",
            "min": "analysis-overlay-min",
            "max": "analysis-overlay-max",
            "reset": "analysis-overlay-reset",
        }
        if overlay
        else {
            "scale": {"axis-scale": pattern},
            "range": {"axis-range": pattern},
            "min": {"axis-min": pattern},
            "max": {"axis-max": pattern},
            "reset": {"axis-reset": pattern},
        }
    )
    return [
        dcc.Dropdown(
            id=ids["scale"],
            options=[
                {"label": "Linear", "value": "linear"},
                {"label": "Log", "value": "log"},
            ],
            value=axis["scale"],
            clearable=False,
        ),
        dcc.RadioItems(
            id=ids["range"],
            options=[
                {"label": "Auto", "value": "auto"},
                {"label": " Custom", "value": "custom"},
            ],
            value=axis["range"],
            inline=True,
        ),
        dcc.Input(
            id=ids["min"],
            type="number",
            value=axis["min"],
            placeholder="min",
            debounce=True,
        ),
        dcc.Input(
            id=ids["max"],
            type="number",
            value=axis["max"],
            placeholder="max",
            debounce=True,
        ),
        html.Button("Reset", id=ids["reset"], n_clicks=0),
    ]


def _note_span(notes: list[str], note_id: Any) -> html.Span:
    return html.Span(
        " · ".join(notes) if notes else "",
        className="panel-note",
        id=note_id,
    )


def _panel_notes(
    series: list[dict[str, Any]], axis: dict[str, Any] | None, display: str
) -> list[str]:
    """Header notes for one panel: coverage, axis facts, and the raw
    series count with its density warning above 100."""
    resolved = figures.resolve_axis(axis, series)
    notes = [*figures.axis_notes(resolved), *figures.count_note(len(series), display)]
    if not series:
        notes = ["no observations under this scope", *notes]
    return notes


def _panel_headers(
    per_key: list[dict[str, Any]],
    axes: dict[str, dict[str, Any]],
    *,
    display: str = "all",
) -> list[html.Div]:
    """Per-key header rows in picker order: title, reorder buttons, the
    compact axis controls, and the coverage/clipping/count notes."""
    headers = []
    for index, entry in enumerate(per_key, start=1):
        key = entry["key"]
        axis = axes.get(key) or default_axis_state()
        notes = _panel_notes(entry["series"], axis, display)
        headers.append(
            html.Div(
                [
                    html.Span(f"{index}. {key}", className="panel-title"),
                    html.Button(
                        "↑", id={"panel-move-up": key}, n_clicks=0, title="Move up"
                    ),
                    html.Button(
                        "↓", id={"panel-move-down": key}, n_clicks=0, title="Move down"
                    ),
                    *_axis_controls(axis, key),
                    _note_span(notes, {"axis-note": key}),
                ],
                className="panel-header",
            )
        )
    return headers


def panel_notes(view_doc: dict[str, Any] | None, data: dict[str, Any] | None) -> list:
    """Note text for every rendered stacked panel, in picker order —
    the ALL-pattern note output realigns every panel without waiting
    for a re-render."""
    doc = view_doc or default_view_state()
    if doc["series"]["mode"] != "stacked":
        return []
    notes = []
    for key in doc["series"]["keys"]:
        series = (data or {}).get("per_key", {}).get(key, {}).get("series", [])
        axis = doc["series"]["axes"].get(key)
        notes.append(
            " · ".join(_panel_notes(series, axis, doc["series"]["trial_display"]))
        )
    return notes


def _overlay_panel(
    per_key: list[dict[str, Any]],
    overlay_axis: dict[str, Any],
    *,
    color: str | None,
    facet: str | None,
    display: str = "all",
    highlighted: Sequence[str] = (),
    grouping: dict | None = None,
) -> list[Any]:
    """The explicit shared-axis overlay: one control block, one figure,
    no per-key axes disturbed."""
    pooled = [series for entry in per_key for series in entry["series"]]
    notes = [
        *figures.axis_notes(figures.resolve_axis(overlay_axis, pooled)),
        *figures.count_note(len(pooled), display),
    ]
    figure = dcc.Graph(
        figure=figures.overlay_figure(
            per_key,
            overlay_axis,
            color=color,
            facet=facet,
            display=display,
            highlighted=highlighted,
            grouping=grouping,
        ),
    )
    return [
        html.Div(
            [
                html.Span("Shared y axis (unnormalized)", className="panel-title"),
                *_axis_controls(overlay_axis, None, overlay=True),
                _note_span(notes, "analysis-overlay-note"),
            ],
            className="panel-header",
        ),
        figure,
    ]


def series_passes_filters(
    series: dict[str, Any], filters: dict[str, list[str]]
) -> bool:
    """A series renders only when every active context filter matches its
    context; a series missing the dimension does not match."""
    context = series.get("context") or {}
    return all(
        str(context.get(dimension)) in values
        for dimension, values in filters.items()
        if values
    )


def apply_context_filters(
    per_key: list[dict[str, Any]], filters: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """The per-key entries after context filtering — filters apply before
    raw, highlighted, and summary rendering alike."""
    return [
        {
            "key": entry["key"],
            "series": [
                series
                for series in entry["series"]
                if series_passes_filters(series, filters)
            ],
        }
        for entry in per_key
    ]


def param_text(value: Any) -> str:
    """Deterministic comparison text for one sampled value; the missing
    marker when the trial never reported it."""
    return MISSING if value is None else figures.value_text(value)


def varying_param_keys(trials: list[dict[str, Any]]) -> list[str]:
    """Param keys whose value or presence differs across the scoped
    trials, in deterministic (sorted) order."""
    keys = {key for trial in trials for key in trial.get("params") or {}}
    return [
        key
        for key in sorted(keys)
        if len({param_text((trial.get("params") or {}).get(key)) for trial in trials})
        > 1
    ]


def param_cardinality(trials: list[dict[str, Any]], key: str) -> int:
    """Distinct comparison texts one param takes across the trials."""
    return len({param_text((trial.get("params") or {}).get(key)) for trial in trials})


def trial_config_text(trial: dict[str, Any], keys: Sequence[str]) -> str:
    """``key=value`` joined text of one trial's varying configuration."""
    return " · ".join(
        f"{key}={param_text((trial.get('params') or {}).get(key))}" for key in keys
    )


def color_dropdown_options(
    dims: list[dict[str, Any]], trials: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Color-by options: logged context dimensions and sampled
    parameters (``param:<key>`` tokens). Flat list — dash 4's dropdown
    renders grouped options as valueless group labels only."""
    return [
        {
            "label": f"{entry['key']} · {entry['cardinality']}",
            "value": entry["key"],
        }
        for entry in dims
    ] + [
        {
            "label": f"param {key} · {param_cardinality(trials, key)}",
            "value": f"param:{key}",
        }
        for key in varying_param_keys(trials)
    ]


def _enrich_series(per_key: list[dict[str, Any]], trials: list[dict[str, Any]]) -> None:
    """Attach each series' trial facts (params, sweep, varying config)
    so coloring, hover, and the browser swatch agree."""
    meta = {trial["trial_id"]: trial for trial in trials}
    varying = varying_param_keys(trials)
    for entry in per_key:
        for series in entry["series"]:
            trial = meta.get(series["trial"]) or {}
            series["params"] = trial.get("params") or {}
            series["sweep"] = trial.get("sweep_id")
            series["config"] = trial_config_text(trial, varying)


def render_series_outputs(
    view_doc: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
) -> tuple[
    list[Any],
    dict[str, Any],
    list[dict[str, str]],
    list[dict[str, str]],
    list[dict[str, str]],
    list[Any],
    str,
]:
    """(panels, payload, key options, color options, facet options,
    context-filter controls, status line) built entirely from the
    snapshot — zero QueryService reads for every view-only edit."""
    doc = view_doc or default_view_state()
    series_doc = doc["series"]
    keys = list(series_doc["keys"])
    payload = snapshot or {}
    stored = payload.get("per_key") or {}
    per_key = [
        {
            "key": key,
            "series": list(stored.get(key, {}).get("series", [])),
        }
        for key in keys
    ]
    dims = payload.get("dims") or []
    trials = payload.get("trials") or []
    grouping = figures.color_grouping(
        [series for entry in per_key for series in entry["series"]],
        series_doc["color"],
    )
    per_key = apply_context_filters(per_key, series_doc["context_filters"])
    display = series_doc["trial_display"]
    highlighted = list(doc["highlighted_trials"])
    if not payload.get("fingerprint"):
        panels = [Empty("Pick a project in the header to analyze its sweeps.")]
    elif not keys:
        panels = [
            Empty(
                "No value keys selected — pick one or more scalar step keys; "
                "the picker order is the panel order."
            )
        ]
    elif series_doc["mode"] == "overlay":
        panels = _overlay_panel(
            per_key,
            series_doc["overlay_axis"],
            color=series_doc["color"],
            facet=series_doc["facet"],
            display=display,
            highlighted=highlighted,
            grouping=grouping,
        )
    else:
        panels = [
            *_panel_headers(per_key, series_doc["axes"], display=display),
            (
                html.Div(
                    Empty(
                        "Highlighted only: click a trace to highlight and "
                        "focus that trial — nothing is highlighted yet, so no "
                        "series render. All raw is one click away; nothing "
                        "switches on its own."
                    ),
                    className="panel-instruction",
                )
                if display == "highlighted" and not highlighted
                else dcc.Graph(
                    figure=figures.stacked_figure(
                        per_key,
                        series_doc["axes"],
                        color=series_doc["color"],
                        facet=series_doc["facet"],
                        display=display,
                        highlighted=highlighted,
                        grouping=grouping,
                    ),
                )
            ),
        ]
    dim_options = [
        {
            "label": f"{entry['key']} · {entry['cardinality']}",
            "value": entry["key"],
        }
        for entry in dims
    ]
    return (
        panels,
        payload,
        list(payload.get("key_options") or []),
        color_dropdown_options(dims, trials),
        dim_options,
        context_filter_controls(dims, series_doc["context_filters"]),
        series_status(
            doc, {"per_key": stored}, incomplete=bool(payload.get("incomplete"))
        ),
    )


def axis_state_edit(
    current: dict[str, Any] | None,
    *,
    metric: str | None,
    control: str,
    scale: Any,
    range_mode: Any,
    low: Any,
    high: Any,
    data: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """(edited view doc, note) after one axis-control edit — ``metric``
    names a per-key stacked axis, ``None`` the shared overlay axis. A
    ``None`` doc means the edit was refused: the last valid axis stays
    and the note says why (invalid bounds, or log against non-positive
    observations — those points are never dropped)."""
    doc = current or default_view_state()
    series = dict(doc["series"])
    if metric is None:
        axis = dict(series["overlay_axis"])
        observations = [
            entry
            for key in series["keys"]
            for entry in (data or {}).get("per_key", {}).get(key, {}).get("series", [])
        ]
    else:
        axis = dict(series["axes"].get(metric) or default_axis_state())
        observations = (data or {}).get("per_key", {}).get(metric, {}).get("series", [])
    field = control if control in {"scale", "range", "min", "max", "reset"} else None
    if field == "reset":
        axis = default_axis_state()
    elif field == "scale":
        axis["scale"] = scale if scale in _AXIS_SCALES else "linear"
    elif field == "range":
        axis["range"] = range_mode if range_mode in _AXIS_RANGES else "auto"
        axis["min"] = _number_or_none(low) if axis["range"] == "custom" else None
        axis["max"] = _number_or_none(high) if axis["range"] == "custom" else None
    elif field in ("min", "max"):
        axis["min"] = _number_or_none(low)
        axis["max"] = _number_or_none(high)
        if axis["min"] is not None and axis["max"] is not None:
            axis["range"] = "custom"
    else:
        return None, None
    if axis["range"] == "custom":
        if axis["min"] is None or axis["max"] is None:
            return None, "custom range needs finite min and max"
        if axis["min"] >= axis["max"]:
            return None, "custom range needs min < max"
        if axis["scale"] == "log" and axis["min"] <= 0:
            return None, "log custom range needs min > 0"
    elif axis["min"] is not None or axis["max"] is not None:
        if field in ("min", "max"):
            return None, "type both finite bounds to apply a custom range"
        axis["min"] = axis["max"] = None
    if axis["scale"] == "log":
        non_positive = figures.non_positive_count(observations)
        if non_positive:
            return (
                None,
                f"log not applied: {non_positive} non-positive observation(s) "
                "— keeping the last valid linear axis",
            )
    resolved = figures.resolve_axis(axis, observations)
    if metric is None:
        series["overlay_axis"] = axis
    else:
        axes = dict(series["axes"])
        if axis == default_axis_state():
            axes.pop(metric, None)
        else:
            axes[metric] = axis
        series["axes"] = axes
    edited = edited_view(doc, {"series": series})
    if edited == doc:
        return None, None
    return edited, " · ".join(figures.axis_notes(resolved))


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def moved_keys(
    current: dict[str, Any] | None, metric: str, direction: str
) -> dict[str, Any] | None:
    """View doc after moving one selected key up/down in picker order;
    ``None`` when the move changes nothing."""
    if direction not in ("up", "down"):
        return None
    doc = current or default_view_state()
    keys = list(doc["series"]["keys"])
    if metric not in keys:
        return None
    index = keys.index(metric)
    target = index - 1 if direction == "up" else index + 1
    if not 0 <= target < len(keys):
        return None
    keys[index], keys[target] = keys[target], keys[index]
    series = dict(doc["series"], keys=keys)
    return edited_view(doc, {"series": series})


def _scalar_text(value: Any) -> str:
    """One final scalar as deterministic cell text."""
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return figures.value_text(value)
    if isinstance(value, dict | list):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _raw_number(value: Any) -> float | None:
    """The numeric sort key for one final scalar; ``None`` sorts last."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _numeric_dim(label: str, values: list[Any]) -> dict[str, Any]:
    """Parcoords dimension over possibly-missing numeric observations."""
    vals = [
        float(value) if _raw_number(value) is not None else math.nan for value in values
    ]
    return {"label": label, "values": vals, "range": figures.padded_range(vals)}


def _label_dim(label: str, labels: Sequence[str]) -> dict[str, Any]:
    """Categorical parcoords dimension with one tick per distinct label."""
    ordered = sorted(set(labels))
    codes = {text: index for index, text in enumerate(ordered)}
    vals = [float(codes[text]) if text in codes else math.nan for text in labels]
    return {
        "label": label,
        "values": vals,
        "range": figures.padded_range(vals),
        "tickvals": list(range(len(ordered))),
        "ticktext": ordered,
    }


def points_scalar_keys(
    service: DashboardService, project: str | None, tray: dict[str, Any] | None
) -> list[str]:
    """Scalar value keys under the scope — the Points view's final-scalar
    columns; a key's final value is its last logged payload, stepped or
    not."""
    if not project or service is None:
        return []
    return sorted(
        {
            entry["key"]
            for entry in service.analysis_value_keys(project, tray)
            if entry["kind"] == "scalar"
        }
    )


def points_view_data(
    trials: list[dict[str, Any]],
    keys: Sequence[str],
    finals: dict[str, dict[str, Any]],
    outcome: str,
) -> dict[str, Any]:
    """(rows, value-key facts, parcoords dims, line identities) for the
    Points view: one row per trial with its final scalars, and the
    params → outcome dimensions over the same set. Line order equals row
    order; a dimension's range always covers every line."""
    ordered = sorted(trials, key=lambda row: (row["sweep_id"], row["number"]))
    numeric = set(figures.numeric_param_keys(ordered))
    varying = [key for key in varying_param_keys(ordered) if key in numeric][
        : figures.MAX_PARAM_DIMS
    ]
    facts = [
        {
            "key": key,
            "numeric": all(
                _raw_number(finals.get(str(trial["trial_id"]), {}).get(key)) is not None
                for trial in ordered
                if key in finals.get(str(trial["trial_id"]), {})
            ),
        }
        for key in keys
    ]
    rows = []
    for trial in ordered:
        trial_id = str(trial["trial_id"])
        per_trial = finals.get(trial_id) or {}
        rows.append(
            {
                "tk": trial_id,
                "sweep": trial.get("sweep_name") or trial["sweep_id"],
                "number": trial["number"],
                "state": trial.get("state") or MISSING,
                **{key: _scalar_text(per_trial.get(key)) for key in keys},
                **{f"{key}_raw": _raw_number(per_trial.get(key)) for key in keys},
            }
        )
    dims = [
        _numeric_dim(
            key,
            [(trial.get("params") or {}).get(key) for trial in ordered],
        )
        for key in varying
    ]
    dims.append(_label_dim("sweep", [str(row["sweep"]) for row in rows]))
    outcome_dim = _numeric_dim(
        f"{outcome} (final)",
        [finals.get(str(row["tk"]), {}).get(outcome) for row in rows],
    )
    dims.append(outcome_dim)
    return {
        "rows": rows,
        "keys": facts,
        "dims": dims,
        "tks": [str(row["tk"]) for row in rows],
        "with_outcome": sum(
            1 for value in outcome_dim["values"] if not math.isnan(value)
        ),
    }


def points_tab(
    service: DashboardService,
    project: str | None,
    tray: dict[str, Any] | None,
    outcome: str,
) -> html.Div:
    """Trials × final scalars with the params → outcome parallel
    coordinates. Selection is client-side state: the table hides
    non-selected rows ("X of Y trials shown"), the parcoords keeps every
    line plotted — brushes fade natively, a selection recolors context
    gray — and axes never rescale."""
    trials = service.analysis_trials(project, tray)
    if not trials:
        return html.Div(
            Empty("No trials under this scope — every member is empty or excluded.")
        )
    view = points_view_data(
        trials,
        points_scalar_keys(service, project, tray),
        service.analysis_finals(project, tray),
        outcome,
    )
    columns: list[SortColumn] = [
        SortColumn("sweep", "Sweep", "string"),
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
    plotted = len(view["dims"]) > 1 and view["with_outcome"]
    return html.Div(
        [
            html.Section(
                [
                    html.H3("Trials · final scalars"),
                    html.P(
                        "The last logged value of each scalar key is the "
                        "trial's final scalar. Click rows or brush the plot "
                        "to select; selected rows stay, the rest hide.",
                        className="hint",
                    ),
                    html.Div(
                        [
                            html.Span(id="inv-points-note", className="series-status"),
                            html.Button(
                                "Clear selection",
                                id="inv-points-clear",
                                n_clicks=0,
                                style={"display": "none"},
                            ),
                        ],
                        className="sel-note-row",
                    ),
                    AgGrid(
                        id="inv-points-grid",
                        rowData=view["rows"],
                        columnDefs=sortable_columns(columns),
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
                    html.H3(f"Params → {outcome} (final)"),
                    *(
                        [
                            dcc.Graph(
                                id="inv-points-figure",
                                figure=figures.points_parcoords(view["dims"]),
                            )
                        ]
                        if plotted
                        else [
                            Empty(
                                "No params → outcome plot: the outcome has no "
                                "numeric final values under this scope."
                            )
                        ]
                    ),
                ],
                className="section",
            ),
            dcc.Store(id="inv-points-data", data={"tks": view["tks"]}),
            dcc.Store(id="inv-points-sel", data={"tks": []}),
            dcc.Store(id="inv-points-echo"),
        ]
    )


def python_snippet(token: str, project: str, base_url: str) -> str:
    """Literally runnable handoff snippet (real client API names)."""
    return (
        "from jernerics.tracking import TrackingClient\n"
        "from jernerics_schema import decode_selection\n"
        "\n"
        f'client = TrackingClient("{base_url}")\n'
        f'selection = decode_selection("{token}")\n'
        f'records = client.project("{project}").values(selection)\n'
    )


def context_filter_controls(
    dims: list[dict[str, Any]], filters: dict[str, list[str]]
) -> list[Any]:
    """One multi-value dropdown per discovered context dimension; values
    come from the view doc, options from unfiltered discovery so filters
    never shrink each other's options."""
    if not dims:
        return [html.Span("No context dimensions under this scope.", className="hint")]
    return [
        html.Div(
            [
                html.Label(dimension["key"], className="filter-label"),
                dcc.Dropdown(
                    id={"context-filter": dimension["key"]},
                    options=[
                        {"label": value, "value": value}
                        for value in dimension["values"]
                    ],
                    value=list(filters.get(dimension["key"]) or []),
                    multi=True,
                    placeholder=f"Filter {dimension['key']}…",
                ),
            ],
            className="context-filter",
        )
        for dimension in dims
    ]


def view_from_context_filter(
    current: dict[str, Any] | None, dimension: str, values: list[str] | None
) -> dict[str, Any]:
    """View doc after one context-filter edit; empty selections drop the
    dimension so the canonical doc never carries no-op filters."""
    doc = current or default_view_state()
    filters = {
        name: list(entries)
        for name, entries in doc["series"]["context_filters"].items()
        if entries
    }
    picked = [str(value) for value in values or [] if str(value)]
    if picked:
        filters[dimension] = picked
    else:
        filters.pop(dimension, None)
    return edited_view(doc, {"series": {**doc["series"], "context_filters": filters}})


_SWATCH_COLUMN: dict[str, Any] = {
    "headerName": "",
    "field": "swatch",
    # AG Grid 35 renders cellRenderer results through React, so a
    # renderer returning a DOM node throws (React error #31) and the
    # whole grid drops its rows; styling the cell itself is safe.
    "cellClass": "trace-swatch-cell",
    "cellStyle": {"function": "params.value ? {background: params.value} : null"},
    "valueFormatter": {"function": "''"},
    "maxWidth": 48,
    "minWidth": 48,
    "sortable": False,
    "filter": False,
    "resizable": False,
    "pinned": "left",
}


def view_from_trace_click(
    current: dict[str, Any] | None, click: dict[str, Any] | None
) -> dict[str, Any] | None:
    """View doc after a trace click: highlight that trial alone — or
    clear the highlight when it was the only one. ``None`` when Plotly
    exposed no identity."""
    points = (click or {}).get("points") or []
    identity = points[0].get("customdata") if points else None
    if not identity:
        return None
    doc = current or default_view_state()
    picked = [str(identity)]
    if doc["highlighted_trials"] == picked:
        picked = []
    return edited_view(doc, {"highlighted_trials": picked})


def series_status(
    doc: dict[str, Any] | None,
    payload: dict[str, Any] | None,
    *,
    incomplete: bool,
) -> str:
    """One line stating both explicit choices — display mode and
    execution reduction — plus the rendered series count and scope
    liveness."""
    doc = doc or default_view_state()
    per_key = (payload or {}).get("per_key", {})
    count = sum(len(entry.get("series", [])) for entry in per_key.values())
    scope = "scope incomplete" if incomplete else "scope terminal"
    return (
        f"display: {doc['series']['trial_display']} · "
        f"reduction: {doc['series']['reduction']} · "
        f"{count} series · {scope}"
    )


def updated_ago(at_ns: int) -> str:
    """The refresh fact line; ages at render time like every page."""
    return f"Updated {relative_time(at_ns)}"


def auto_refresh_flip(
    view_doc: dict[str, Any] | None, incomplete: bool
) -> dict[str, Any] | None:
    """Doc clearing auto-refresh once the scope turned terminal; ``None``
    keeps the persisted intent."""
    if not view_doc or not view_doc.get("auto_refresh") or incomplete:
        return None
    return edited_view(view_doc, {"auto_refresh": False})


def extract_series_figure(panels: list[Any]) -> tuple[list[Any], Any]:
    """(panels without the embedded graph, its figure): the callback
    writes the figure to the layout-level graph, so Plotly's uirevision
    keeps user zoom across refreshes instead of losing a replaced div."""
    for index, node in enumerate(panels):
        if isinstance(node, dcc.Graph):
            return [*panels[:index], *panels[index + 1 :]], node.figure
    return panels, figures.empty_figure()


def series_data_outputs(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
    now_ns: int,
) -> tuple[dict[str, Any], str]:
    """The data callback's outputs: the fresh canonical snapshot and the
    updated-ago line."""
    return (
        series_snapshot(service, project, tray, view_doc, now_ns),
        updated_ago(now_ns),
    )


def series_data_failure(error: Exception, now_ns: int) -> tuple[Any, ...]:
    """The data callback's no_update tuple for a failed refresh: the
    last successful snapshot and presentation survive, and the error
    replaces the status line until a good refresh."""
    return (
        no_update,
        no_update,
        f"refresh failed — keeping the last successful view: {error}",
    )


def series_view_outputs(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
    snapshot: dict[str, Any] | None,
    now_ns: int,
) -> tuple[Any, ...]:
    """The view callback's outputs: the presentation tuple (panels,
    snapshot to persist, key/color/facet options, filter controls,
    status, figure) rebuilt from the stored snapshot. View-only edits
    reuse it with zero reads; added keys fetch only the missing ones;
    scope/reduction changes rebuild. ``no_update`` in slot 2 means the
    stored snapshot already serves the view."""
    doc = view_doc or default_view_state()
    series_doc = doc["series"]
    usable, missing = snapshot_status(
        snapshot,
        scope_fingerprint(project, tray),
        series_doc["reduction"],
        series_doc["keys"],
    )
    if not usable:
        snapshot = series_snapshot(service, project, tray, doc, now_ns)
        persist = snapshot
    elif missing and service is not None and snapshot is not None:
        snapshot = merge_series_keys(
            service, project or "", tray, snapshot, missing, now_ns
        )
        persist = snapshot
    else:
        persist = no_update
    panels, _payload, key_options, color_options, facet_options, filters, status = (
        render_series_outputs(doc, snapshot)
    )
    panels, figure = extract_series_figure(panels)
    return (
        panels,
        persist,
        key_options,
        color_options,
        facet_options,
        filters,
        status,
        figure,
    )


def series_view_failure(error: Exception) -> tuple[Any, ...]:
    """The view callback's no_update tuple for a failed render: the last
    successful presentation survives and the error replaces the status
    line."""
    message = f"refresh failed — keeping the last successful view: {error}"
    return (
        no_update,
        no_update,
        no_update,
        no_update,
        no_update,
        Error(message),
        no_update,
        no_update,
    )
