"""Analysis page: cross-sweep exploration and comparison (jernerics-h5d.13).

One project's sweeps, trials, and retry families feed a shared selection
tray; the effective selection round-trips through the URL query string
(``?sel=<token>``) with the same token format the jernerics client
uses, so dashboards and Python sessions hand selections to each other.
A persistent scope bar plus an Edit-scope disclosure sit above every
view, and the active view and its controls ride a second, dashboard-
only ``?view=<json>`` parameter (jernerics-cdf.3). Tabs: data catalog,
series overlay, points tables, study-style Optuna views (plain plotly —
no optuna dependency), and the continue-in-Python handoff. All data
flows through DashboardService.
"""

import json
import math
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, quote

from dash import dcc, html, no_update
from dash_ag_grid import AgGrid
from jernerics_schema import Selection

from . import components, figures
from .components import MISSING, Empty, Error, relative_time, short_id
from .routes import ROUTES_BASE, parse_route
from .selection_tokens import (
    SelectionTokenError,
    decode_selection_token,
    encode_selection_token,
)
from .service import ANALYSIS_REDUCTIONS, DashboardService

EMPTY_TRAY: dict[str, Any] = {
    "project": None,
    "sweeps": [],
    "trials": [],
    "families": [],
    "executions": [],
    "expand": False,
}
"""Shape of the unified selection store: the active project, sweep ids,
explicit trial ids, picked retry-family roots, explicit execution ids,
and the per-family expansion toggle. The workspace sweep grid and the
analysis pickers all read and write this one shape."""

_GRID_DEFAULTS: dict[str, Any] = {
    "sortable": True,
    "filter": True,
    "resizable": True,
    "minWidth": 100,
}

VIEW_VERSION = 1
"""Wire version of the dashboard-only ``view=`` URL document."""

_ANALYSIS_VIEWS = ("overview", "catalog", "series", "points", "optuna", "python")
_SERIES_MODES = ("stacked", "overlay")
_TRIAL_DISPLAYS = ("all", "highlighted", "median_iqr")
_AXIS_SCALES = ("linear", "log")
_AXIS_RANGES = ("auto", "custom")
_FOCUS_KINDS = ("sweep", "trial", "execution")


class ViewStateError(Exception):
    """The ``view=`` URL parameter is malformed or unsupported."""


def default_axis_state() -> dict[str, Any]:
    """A per-key y-axis at its default: linear scale, auto range."""
    return {"scale": "linear", "range": "auto", "min": None, "max": None}


def default_view_state() -> dict[str, Any]:
    """The v1 view document with every control at its default."""
    return {
        "v": VIEW_VERSION,
        "active": "overview",
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
        "highlighted_trials": [],
        "focus": None,
        "auto_refresh": False,
        "include_archived": False,
        "include_invalid": False,
        "optuna": {"contour_x": None, "contour_y": None},
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ViewStateError(message)


def _optional_key(value: Any, field: str) -> str | None:
    """A string-or-null field; empty or non-string values are errors."""
    if value is None:
        return None
    _require(
        isinstance(value, str) and value != "",
        f"{field} must be a non-empty string or null",
    )
    return value


def _string_list(value: Any, field: str) -> list[str]:
    _require(isinstance(value, list), f"{field} must be a list")
    for item in value:
        _require(
            isinstance(item, str) and item != "",
            f"{field} entries must be non-empty strings",
        )
    return list(value)


def _finite_number(value: Any, field: str) -> float | None:
    """A finite numeric field; ``None`` when absent, anything else is an
    error."""
    if value is None:
        return None
    _require(
        isinstance(value, int | float) and not isinstance(value, bool),
        f"{field} must be a finite number",
    )
    _require(math.isfinite(value), f"{field} must be a finite number")
    return float(value)


def decode_axis_state(value: Any, field: str) -> dict[str, Any]:
    """A validated per-key axis object; unknown fields are dropped and
    ``auto`` ranges ignore stored bounds."""
    _require(isinstance(value, dict), f"{field} must be an object")
    scale = value.get("scale", "linear")
    _require(scale in _AXIS_SCALES, f"{field}.scale must be linear or log")
    range_mode = value.get("range", "auto")
    _require(range_mode in _AXIS_RANGES, f"{field}.range must be auto or custom")
    axis = {"scale": scale, "range": range_mode, "min": None, "max": None}
    if range_mode == "custom":
        low = _finite_number(value.get("min"), f"{field}.min")
        high = _finite_number(value.get("max"), f"{field}.max")
        if low is None or high is None:
            raise ViewStateError(f"{field} custom range requires finite min and max")
        _require(low < high, f"{field} custom range requires min < max")
        if scale == "log":
            _require(low > 0, f"{field} log custom range requires min > 0")
        axis["min"], axis["max"] = low, high
    return axis


def decode_view_state(raw: str) -> dict[str, Any]:
    """The canonical v1 view document from a ``view=`` value; unknown
    fields are dropped, missing fields take defaults, wrong types or
    enum values raise :class:`ViewStateError`."""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise ViewStateError(f"view state is malformed: {error}") from error
    _require(isinstance(payload, dict), "view state must be a JSON object")
    version = payload.get("v")
    _require(
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == VIEW_VERSION,
        f"unsupported view state: expected version {VIEW_VERSION}",
    )
    doc = default_view_state()
    active = payload.get("active", doc["active"])
    _require(
        isinstance(active, str) and active in _ANALYSIS_VIEWS,
        f"unsupported analysis view {active!r}",
    )
    doc["active"] = active
    series = payload.get("series", {})
    _require(isinstance(series, dict), "series must be an object")
    doc["series"]["keys"] = list(
        dict.fromkeys(_string_list(series.get("keys", []), "series.keys"))
    )
    mode = series.get("mode", doc["series"]["mode"])
    _require(mode in _SERIES_MODES, f"unsupported series mode {mode!r}")
    doc["series"]["mode"] = mode
    reduction = series.get("reduction", doc["series"]["reduction"])
    _require(
        reduction in ANALYSIS_REDUCTIONS,
        f"unsupported execution reduction {reduction!r}",
    )
    doc["series"]["reduction"] = reduction
    trial_display = series.get("trial_display")
    if trial_display is None:
        trial_display = "all"
    _require(
        trial_display in _TRIAL_DISPLAYS,
        f"unsupported trial display {trial_display!r}",
    )
    doc["series"]["trial_display"] = trial_display
    filters = series.get("context_filters", {})
    _require(isinstance(filters, dict), "series.context_filters must be an object")
    for name, values in filters.items():
        _require(
            isinstance(name, str) and name != "",
            "series.context_filters keys must be non-empty strings",
        )
        doc["series"]["context_filters"][name] = _string_list(
            values, f"series.context_filters[{name!r}]"
        )
    doc["series"]["color"] = _optional_key(series.get("color"), "series.color")
    doc["series"]["facet"] = _optional_key(series.get("facet"), "series.facet")
    axes = series.get("axes", {})
    _require(isinstance(axes, dict), "series.axes must be an object")
    for name, axis in axes.items():
        _require(
            isinstance(name, str) and name != "",
            "series.axes keys must be non-empty strings",
        )
        doc["series"]["axes"][name] = decode_axis_state(axis, f"series.axes[{name!r}]")
    doc["series"]["overlay_axis"] = decode_axis_state(
        series.get("overlay_axis", default_axis_state()), "series.overlay_axis"
    )
    doc["highlighted_trials"] = _string_list(
        payload.get("highlighted_trials", []), "highlighted_trials"
    )
    doc["focus"] = decode_focus(payload.get("focus"))
    auto_refresh = payload.get("auto_refresh", False)
    _require(isinstance(auto_refresh, bool), "auto_refresh must be a boolean")
    doc["auto_refresh"] = auto_refresh
    include_archived = payload.get("include_archived", False)
    _require(isinstance(include_archived, bool), "include_archived must be a boolean")
    doc["include_archived"] = include_archived
    include_invalid = payload.get("include_invalid", False)
    _require(isinstance(include_invalid, bool), "include_invalid must be a boolean")
    doc["include_invalid"] = include_invalid
    optuna = payload.get("optuna", {})
    _require(isinstance(optuna, dict), "optuna must be an object")
    doc["optuna"]["contour_x"] = _optional_key(
        optuna.get("contour_x"), "optuna.contour_x"
    )
    doc["optuna"]["contour_y"] = _optional_key(
        optuna.get("contour_y"), "optuna.contour_y"
    )
    return doc


def encode_view_state(doc: dict[str, Any]) -> str:
    """Percent-encoded compact JSON for the ``view=`` URL parameter."""
    return quote(json.dumps(doc, separators=(",", ":"), sort_keys=True), safe="")


def decode_focus(value: Any) -> dict[str, str] | None:
    """A validated focus object ``{kind, id}``; ``None`` when absent."""
    if value is None:
        return None
    _require(isinstance(value, dict), "focus must be an object")
    kind = value.get("kind")
    _require(kind in _FOCUS_KINDS, "focus.kind must be sweep, trial, or execution")
    focus_id = _optional_key(value.get("id"), "focus.id")
    _require(focus_id is not None, "focus.id must be a non-empty string")
    return {"kind": kind, "id": focus_id}


def with_focus(
    current: dict[str, Any] | None, focus: dict[str, str] | None
) -> dict[str, Any]:
    """View doc after a focus edit; nothing but ``focus`` changes — focus
    edits never narrow scope."""
    return {**(current or default_view_state()), "focus": focus}


def _view_param(search: str | None) -> str | None:
    values = parse_qs((search or "").lstrip("?")).get("view")
    return values[0] if values else None


def hydrate_view(
    pathname: str | None,
    search: str | None,
    current: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """(doc, error) for a URL carrying ``?view=``. ``None`` means leave
    the view store alone (off the workspace route, or already showing
    this state). No parameter means defaults; a malformed or unsupported
    document yields defaults plus a visible error."""
    if parse_route(pathname).kind != "workspace":
        return None, None
    raw = _view_param(search)
    defaults = default_view_state()
    if raw is None:
        return (None if current == defaults else defaults), None
    try:
        doc = decode_view_state(raw)
    except ViewStateError as error:
        return defaults, str(error)
    return (None if current == doc else doc), None


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
    """(active, keys, mode, reduction, color, facet, contour_x,
    contour_y, trial_display, auto_refresh) the analysis controls take
    from the view state; dropdown values arrive only once their options
    carry them."""
    doc = doc or default_view_state()
    return (
        doc["active"],
        _gated_keys(doc["series"]["keys"], loaded.get("keys")),
        doc["series"]["mode"],
        doc["series"]["reduction"],
        _gated_value(doc["series"]["color"], loaded.get("color")),
        _gated_value(doc["series"]["facet"], loaded.get("facet")),
        _gated_value(doc["optuna"]["contour_x"], loaded.get("contour_x")),
        _gated_value(doc["optuna"]["contour_y"], loaded.get("contour_y")),
        doc["series"]["trial_display"],
        ["auto"] if doc["auto_refresh"] else [],
    )


def view_from_controls(
    current: dict[str, Any] | None,
    *,
    active: str | None,
    keys: list[str] | None,
    mode: str | None,
    reduction: str | None,
    color: str | None,
    facet: str | None,
    contour_x: str | None,
    contour_y: str | None,
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
    optuna = dict(doc["optuna"])
    if "contour_x" in edited:
        optuna["contour_x"] = contour_x or None
    if "contour_y" in edited:
        optuna["contour_y"] = contour_y or None
    auto_refresh_state = bool(doc["auto_refresh"])
    if "auto_refresh" in edited and auto_refresh is not None:
        auto_refresh_state = bool(auto_refresh)
    return {
        **doc,
        "active": (
            active
            if "active" in edited and active in _ANALYSIS_VIEWS
            else doc["active"]
        ),
        "series": series,
        "optuna": optuna,
        "auto_refresh": auto_refresh_state,
    }


_CONTROL_IDS = {
    "analysis-tabs": "active",
    "analysis-key": "keys",
    "analysis-mode": "mode",
    "analysis-reduction": "reduction",
    "analysis-display": "trial_display",
    "analysis-auto-refresh": "auto_refresh",
    "analysis-color": "color",
    "analysis-facet": "facet",
    "analysis-contour-x": "contour_x",
    "analysis-contour-y": "contour_y",
}


def edited_fields(triggered: Any) -> set[str]:
    """View-doc fields named by the triggered callback inputs."""
    return {
        field
        for field in (
            _CONTROL_IDS.get(str(prop).split(".", 1)[0]) for prop in triggered
        )
        if field
    }


def family_picker_rows(
    families: list[dict[str, Any]], tray: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Family-picker grid rows, selected rows matching the tray's roots."""
    rows = [
        {
            "root": row["root"],
            "root_short": short_id(row["root"]),
            "current_short": short_id(row["current_trial"]),
            "number": row["number"],
            "state": row["state"],
            "objective": components.objective_text(row["objective"]),
            "generations": row["generations"],
        }
        for row in families
    ]
    picked = set((tray or {}).get("families") or [])
    return rows, [row for row in rows if row["root"] in picked]


def tray_summary(tray: dict[str, Any] | None) -> str:
    tray = tray or EMPTY_TRAY
    parts = [
        f"{len(tray.get('sweeps') or [])} sweep(s)",
        f"{len(tray.get('trials') or [])} trial(s)",
        f"{len(tray.get('families') or [])} family/families",
    ]
    if tray.get("executions"):
        parts.append(f"{len(tray['executions'])} execution(s)")
    if tray.get("expand"):
        parts.append("retry families expanded")
    return " · ".join(parts)


def tray_from_edit(
    sweep_rows: list[dict[str, Any]] | None,
    family_rows: list[dict[str, Any]] | None,
    expand_values: list[str] | None,
    current: dict[str, Any] | None,
    *,
    sweep_edited: bool,
    family_edited: bool,
    expand_edited: bool,
) -> dict[str, Any]:
    """Merge grid/expand edits into the unified selection store; the
    active project and explicit trials/executions (kept from a hydrated
    token) survive edits.

    Only the control the event actually carried is authoritative for its
    dimension — every other dimension keeps the current tray. A grid
    event fires while the OTHER grid may still hold a stale selection
    snapshot (AG Grid applies programmatic selectedRows per grid, not
    atomically), and a mount echo of the pre-hydration state must not
    erase the dimensions the user did not touch (jernerics-8c9)."""
    current = current or EMPTY_TRAY
    return {
        "project": current.get("project"),
        "sweeps": (
            sorted({str(row["sweep_id"]) for row in sweep_rows or []})
            if sweep_edited
            else list(current.get("sweeps") or [])
        ),
        "trials": list(current.get("trials") or []),
        "families": (
            sorted({str(row["root"]) for row in family_rows or []})
            if family_edited
            else list(current.get("families") or [])
        ),
        "executions": list(current.get("executions") or []),
        "expand": (
            bool(expand_values) if expand_edited else bool(current.get("expand"))
        ),
    }


def mounted_selection(selected: list[Any], *, initial: bool) -> Any:
    """Selection to push to a freshly mounted grid. An empty selection
    write on mount is redundant — the grid starts unselected — and its
    echo fires as an edit against a tray hydration may have landed in
    between, wiping it (jernerics-8c9). Non-empty selections and real
    clears (post-mount, e.g. hydrating a token that drops a dimension)
    pass through untouched."""
    return no_update if initial and not selected else selected


def tray_from_selection(selection: Any) -> dict[str, Any]:
    """Unified selection store matching a decoded token selection.

    Retry roots hydrate as picked families with the expansion toggle on:
    that is exactly what a retry-root selection means, and it keeps the
    hydrated tray's effective selection equal to the decoded one.
    """
    return {
        "project": selection.project,
        "sweeps": [str(value) for value in selection.sweeps or ()],
        "trials": [str(value) for value in selection.trials or ()],
        "families": [str(value) for value in selection.retry_roots or ()],
        "executions": [str(value) for value in selection.executions or ()],
        "expand": bool(selection.retry_roots),
    }


def _sel_param(search: str | None) -> str | None:
    values = parse_qs((search or "").lstrip("?")).get("sel")
    return values[0] if values else None


def hydrate_tray(
    service: DashboardService,
    project: str | None,
    pathname: str | None,
    search: str | None,
    current: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """(tray, error) for a URL carrying ``?sel=``. A ``None`` tray means
    "leave the current state alone" (no token, a different page, or a
    token equal to what is already shown). A token scoped to another
    project surfaces as an error instead of mixing. With no project
    picked, the token only decides the cold start (jernerics-xbx): the
    shell adopts the token's project through the picker and hydration
    re-fires when project-store settles, while a token the dashboard
    cannot act on surfaces its error instead of silently empty grids."""
    token = _sel_param(search)
    if not token or parse_route(pathname).kind != "workspace":
        return None, None
    if not project:
        _selection, error = cold_start(service, search)
        return None, error
    try:
        selection = decode_selection_token(token, project=project)
    except SelectionTokenError as error:
        return None, str(error)
    if current and service.analysis_selection(project, current) == selection:
        return None, None
    return tray_from_selection(selection), None


def cold_start(
    service: DashboardService, search: str | None
) -> tuple[Selection | None, str | None]:
    """(selection, error) a shared token offers a session with no project
    picked: the decoded selection when its project is known here — the
    picker adopts it, running the same settle path as a manual pick —
    an error naming a project this dashboard has no data for, or a
    decode error. ``(None, None)`` when the URL carries no token."""
    token = _sel_param(search)
    if not token:
        return None, None
    try:
        selection = decode_selection_token(token)
    except SelectionTokenError as error:
        return None, str(error)
    if selection.project not in service.projects():
        return (
            None,
            f"selection token targets project {selection.project!r}, which "
            "this dashboard has no data for; pick a project to analyze.",
        )
    return selection, None


def expand_values(tray: dict[str, Any] | None) -> list[str]:
    """Expansion-toggle checklist values matching the tray's flag."""
    return ["expand"] if (tray or {}).get("expand") else []


def include_values(doc: dict[str, Any] | None) -> list[str]:
    """Include-control checklist values matching the view state."""
    doc = doc or default_view_state()
    values = []
    if doc.get("include_archived"):
        values.append("archived")
    if doc.get("include_invalid"):
        values.append("invalid")
    return values


def view_from_include(
    current: dict[str, Any] | None, values: list[str] | None
) -> dict[str, Any]:
    """View state after an include-control edit; only the two include
    flags change."""
    picked = set(values or [])
    return {
        **(current or default_view_state()),
        "include_archived": "archived" in picked,
        "include_invalid": "invalid" in picked,
    }


def synced_search(
    service: DashboardService,
    pathname: str | None,
    tray: dict[str, Any] | None,
    current_search: str | None,
    project: str | None,
    *,
    view_doc: dict[str, Any] | None = None,
    url_navigated: bool,
) -> str | None:
    """The URL search after a navigation or a tray/view edit; ``None``
    leaves it alone. Navigations may only drop the workspace parameters —
    minting on navigation would let a stale session tray clobber a
    freshly opened deep link before hydration lands. Tray and view
    edits mint, and only on the workspace page."""
    if url_navigated:
        if current_search and parse_route(pathname).kind != "workspace":
            return ""
        return None
    if parse_route(pathname).kind != "workspace":
        return None
    return search_from_state(service, project, tray, view_doc, current_search)


def selection_query(
    service: DashboardService, project: str | None, tray: dict[str, Any] | None
) -> str:
    """The ``sel=`` query fragment; empty when the tray has nothing to
    hand off or no project is active."""
    if not project:
        return ""
    picks = tray or EMPTY_TRAY
    if not any(
        picks.get(name) for name in ("sweeps", "trials", "families", "executions")
    ):
        return ""
    token = encode_selection_token(service.analysis_selection(project, picks))
    return f"sel={token}"


def view_query(view_doc: dict[str, Any] | None) -> str:
    """The ``view=`` query fragment; empty when the state is absent or
    default (a default state does not belong in the URL)."""
    if not view_doc or view_doc == default_view_state():
        return ""
    return f"view={encode_view_state(view_doc)}"


def _query_search(fragments: list[str]) -> str:
    joined = "&".join(fragment for fragment in fragments if fragment)
    return f"?{joined}" if joined else ""


def search_from_tray(
    service: DashboardService,
    project: str | None,
    tray: dict[str, Any] | None,
    current_search: str | None,
) -> str | None:
    """URL search carrying the tray's effective selection; ``None`` when
    unchanged. An empty tray clears the token (nothing to hand off)."""
    if not project:
        return None
    target = _query_search([selection_query(service, project, tray)])
    return None if target == (current_search or "") else target


def search_from_state(
    service: DashboardService,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
    current_search: str | None,
) -> str | None:
    """URL search carrying both parameters; ``None`` when unchanged. A
    current ``view=`` that does not decode is left in place (the visible
    error stays until a real edit rewrites it)."""
    target = _query_search(
        [
            fragment
            for fragment in (
                selection_query(service, project, tray),
                view_query(view_doc),
            )
            if fragment
        ]
    )
    if target == (current_search or ""):
        return None
    if "view=" not in target and _view_param(current_search) is not None:
        try:
            decode_view_state(_view_param(current_search) or "")
        except ViewStateError:
            return None
    return target


def workspace_focus_href(project: str, kind: str, object_id: str) -> str:
    """Workspace URL whose view document focuses one object (the
    artifact viewer's back-links)."""
    doc = dict(default_view_state(), focus={"kind": kind, "id": object_id})
    return f"{ROUTES_BASE}/project/{project}?view={encode_view_state(doc)}"


def _pick_project_first() -> html.Div:
    return html.Div(Empty("Pick a project in the header to analyze its sweeps."))


def catalog_tab(
    service: DashboardService, project: str | None, tray: dict[str, Any] | None
) -> html.Div:
    """Discovered facts for the tray: value keys, context dimensions,
    param coverage per sweep, artifact keys — no metric-name heuristics."""
    if not project:
        return _pick_project_first()
    values = service.analysis_value_keys(project, tray)
    context = service.analysis_context_dims(project, tray)
    coverage = service.analysis_param_coverage(project, tray)
    artifacts = service.analysis_artifacts(project, tray)
    sweep_names = [coverage["names"].get(sweep, sweep) for sweep in coverage["sweeps"]]
    return html.Div(
        [
            html.Section(
                [
                    html.H3("Value keys"),
                    components.DataTable(
                        ("Key", "Kind", "Points", "Steps", "Trials", "Families"),
                        [
                            (
                                entry["key"],
                                entry["kind"],
                                entry["points"],
                                (
                                    f"{entry['extent'][0]}-{entry['extent'][1]}"
                                    if entry["steps"]
                                    else "no"
                                ),
                                entry["trials"],
                                entry["families"],
                            )
                            for entry in values
                        ],
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Context dimensions"),
                    components.DataTable(
                        ("Dimension", "Distinct values", "Samples"),
                        [
                            (
                                entry["key"],
                                entry["cardinality"],
                                ", ".join(map(str, entry["samples"])),
                            )
                            for entry in context
                        ],
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Param coverage per sweep"),
                    components.DataTable(
                        ("Param", *sweep_names),
                        [
                            (
                                entry["key"],
                                *(
                                    f"{cell['trials']} ({cell['kinds']})"
                                    if cell
                                    else MISSING
                                    for cell in entry["cells"].values()
                                ),
                            )
                            for entry in coverage["rows"]
                        ],
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Artifact keys"),
                    components.DataTable(
                        ("Key", "Count", "Sources"),
                        [
                            (e["key"], e["count"], ", ".join(e["sources"]))
                            for e in artifacts
                        ],
                    ),
                ],
                className="section",
            ),
        ],
    )


def _coverage_label(entry: dict[str, Any]) -> str:
    low, high = entry["extent"]
    extent = f"steps {low}-{high}" if entry["steps"] else "no steps beyond 0"
    return (
        f"{entry['key']} · {entry['kind']} · {entry['points']} pts · "
        f"{entry['trials']} trial(s) · {entry['families']} family/families · "
        f"{extent}"
    )


def _series_payload(per_key: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
    """The analysis-series-data store payload: every fetched observation
    behind the panels, for axis edits that must not re-query."""
    per_key_data = {key: {"series": []} for key in keys}
    per_key_data.update(
        {entry["key"]: {"series": entry["series"]} for entry in per_key}
    )
    return {"keys": keys, "per_key": per_key_data}


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
    """Grouped Color-by options: logged context dimensions and sampled
    parameters (``param:<key>`` tokens)."""
    groups = [
        {
            "label": "Logged context",
            "options": [
                {
                    "label": f"{entry['key']} · {entry['cardinality']}",
                    "value": entry["key"],
                }
                for entry in dims
            ],
        },
        {
            "label": "Sampled parameters",
            "options": [
                {
                    "label": f"{key} · {param_cardinality(trials, key)}",
                    "value": f"param:{key}",
                }
                for key in varying_param_keys(trials)
            ],
        },
    ]
    return [group for group in groups if group["options"]]


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


def series_outputs(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
) -> tuple[
    list[Any], dict, list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]
]:
    """(panels, data payload, key options, color options, facet options)
    for the series tab — the callback's output order: every panel comes
    from ONE multi-key values read; per-key axis state, context filters,
    display mode, and highlights ride the view doc."""
    doc = view_doc or default_view_state()
    series_doc = doc["series"]
    keys = list(series_doc["keys"])
    if not project or service is None:
        return (
            [Empty("Pick a project in the header to analyze its sweeps.")],
            _series_payload([], keys),
            [],
            [],
            [],
        )
    coverage = service.analysis_value_keys(project, tray)
    offered = {
        entry["key"]: _coverage_label(entry)
        for entry in coverage
        if entry["kind"] == "scalar" and entry["steps"]
    }
    key_options = [
        {"label": offered.get(key, f"{key} · absent under this scope"), "value": key}
        for key in sorted({*offered, *keys})
    ]
    dims = service.analysis_context_dims(project, tray)
    trials = service.analysis_trials(project, tray)
    dim_options = [
        {
            "label": f"{entry['key']} · {entry['cardinality']}",
            "value": entry["key"],
        }
        for entry in dims
    ]
    per_key = service.analysis_series(
        project, tray, keys, series_doc["reduction"] or "none"
    )
    _enrich_series(per_key, trials)
    grouping = figures.color_grouping(
        [series for entry in per_key for series in entry["series"]],
        series_doc["color"],
    )
    per_key = apply_context_filters(per_key, series_doc["context_filters"])
    payload = _series_payload(per_key, keys)
    display = series_doc["trial_display"]
    highlighted = list(doc["highlighted_trials"])
    if not keys:
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
    return (
        panels,
        payload,
        key_options,
        color_dropdown_options(dims, trials),
        dim_options,
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
    edited = {**doc, "series": series}
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
    return {**doc, "series": series}


def _format_payload(payload: Any) -> str:
    if isinstance(payload, dict | list):
        return json.dumps(payload, indent=2, sort_keys=True)
    if payload is None:
        return MISSING
    if isinstance(payload, bool):
        return "true" if payload else "false"
    return str(payload)


def _cell(payloads: list[Any] | None) -> str:
    """All logged payloads for one (trial, key); missing cells render as
    the em dash marker."""
    if not payloads:
        return MISSING
    return "\n---\n".join(_format_payload(payload) for payload in payloads)


def points_tab(
    service: DashboardService, project: str | None, tray: dict[str, Any] | None
) -> html.Div:
    """AG Grid tables: non-step scalar/JSON point values and param
    comparison, with per-column presence counts and "—" for missing."""
    if not project:
        return _pick_project_first()
    data = service.analysis_points(project, tray)
    if not data["trials"]:
        return html.Div(Empty("The tray is empty — pick sweeps or families first."))
    labels = [
        f"#{trial['number']} {short_id(trial['trial_id'])}" for trial in data["trials"]
    ]
    value_columns: list[dict[str, Any]] = [
        {"headerName": "Trial", "field": "trial", "pinned": "left"}
    ]
    for entry in data["value_keys"]:
        present = sum(
            1
            for trial in data["trials"]
            if entry["key"] in data["values"].get(trial["trial_id"], {})
        )
        value_columns.append(
            {
                "headerName": (
                    f"{entry['key']} · {entry['kind']} · {present}/{len(labels)}"
                ),
                "field": entry["key"],
            }
        )
    value_rows = []
    for trial, label in zip(data["trials"], labels, strict=True):
        payloads = data["values"].get(trial["trial_id"], {})
        value_rows.append(
            {
                "trial": label,
                **{
                    entry["key"]: _cell(payloads.get(entry["key"]))
                    for entry in data["value_keys"]
                },
            }
        )
    param_columns: list[dict[str, Any]] = [
        {"headerName": "Trial", "field": "trial", "pinned": "left"}
    ]
    for key in data["param_keys"]:
        present = sum(1 for per_trial in data["params"].values() if key in per_trial)
        param_columns.append(
            {
                "headerName": f"{key} · {present}/{len(labels)}",
                "field": key,
            }
        )
    param_rows = []
    for trial, label in zip(data["trials"], labels, strict=True):
        per_trial = data["params"].get(trial["trial_id"], {})
        param_rows.append(
            {
                "trial": label,
                **{
                    key: (
                        _format_payload(per_trial[key]) if key in per_trial else MISSING
                    )
                    for key in data["param_keys"]
                },
            }
        )
    return html.Div(
        [
            html.Section(
                [
                    html.H3("Point values (non-step keys)"),
                    AgGrid(
                        rowData=value_rows,
                        columnDefs=value_columns,
                        defaultColDef=_GRID_DEFAULTS,
                        dashGridOptions=components.grid_options(),
                        className="ag-theme-alpine grid",
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Params"),
                    AgGrid(
                        rowData=param_rows,
                        columnDefs=param_columns,
                        defaultColDef=_GRID_DEFAULTS,
                        dashGridOptions=components.grid_options(),
                        className="ag-theme-alpine grid",
                    ),
                ],
                className="section",
            ),
        ]
    )


def optuna_tab_content(
    service: DashboardService,
    project: str | None,
    tray: dict[str, Any] | None,
    x_param: str | None,
    y_param: str | None,
) -> tuple[html.Div, list[dict[str, str]], list[dict[str, str]]]:
    """(container, contour-x options, contour-y options): one figure set
    per selected sweep, built from canonical trial snapshots."""
    if not project:
        return _pick_project_first(), [], []
    rows = service.analysis_trials(project, tray)
    if not rows:
        return (
            html.Div(Empty("The tray is empty — pick sweeps or families first.")),
            [],
            [],
        )
    grouped: dict[str, list[dict[str, Any]]] = {}
    names: dict[str, str] = {}
    for row in rows:
        grouped.setdefault(row["sweep_id"], []).append(row)
        names[row["sweep_id"]] = row["sweep_name"]
    sections = []
    for sweep_id in sorted(grouped):
        sweep_rows = grouped[sweep_id]
        numeric = figures.numeric_param_keys(sweep_rows)
        if len(numeric) >= 2:
            chosen_x = x_param if x_param in numeric else numeric[0]
            chosen_y = y_param if y_param in numeric else numeric[1]
            contour: Any = dcc.Graph(
                figure=figures.contour_figure(sweep_rows, chosen_x, chosen_y)
            )
            contour_title = f"Contour · {chosen_x} x {chosen_y}"
        else:
            contour = Empty(
                f"Contour needs at least two numeric sampled params; sweep "
                f"{names[sweep_id]} has {len(numeric)}."
            )
            contour_title = "Contour"
        sections.append(
            html.Section(
                [
                    html.H3(f"Sweep {names[sweep_id]} · {short_id(sweep_id)}"),
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H4("Optimization history"),
                                    dcc.Graph(
                                        figure=figures.optimization_history(sweep_rows)
                                    ),
                                ]
                            ),
                            html.Div(
                                [
                                    html.H4("Parallel coordinates"),
                                    dcc.Graph(
                                        figure=figures.parallel_coordinates(sweep_rows)
                                    ),
                                ]
                            ),
                            html.Div(
                                [
                                    html.H4("Slice"),
                                    dcc.Graph(figure=figures.slice_figure(sweep_rows)),
                                ]
                            ),
                            html.Div([html.H4(contour_title), contour]),
                            html.Div(
                                [
                                    html.H4("Timeline"),
                                    dcc.Graph(
                                        figure=figures.trial_timeline(sweep_rows)
                                    ),
                                ]
                            ),
                        ],
                        className="figure-grid",
                    ),
                ],
                className="section",
            )
        )
    options = [{"label": key, "value": key} for key in figures.numeric_param_keys(rows)]
    return html.Div(sections), options, options


def python_snippet(token: str, project: str) -> str:
    """Literally runnable handoff snippet (real client API names)."""
    return (
        "from jernerics.tracking import TrackingClient\n"
        "from jernerics.tracking.client import decode_selection\n"
        "\n"
        'client = TrackingClient("http://localhost:8000")\n'
        f'selection = decode_selection("{token}")\n'
        f'records = client.project("{project}").values(selection)\n'
    )


def python_tab(
    service: DashboardService, project: str | None, tray: dict[str, Any] | None
) -> html.Div:
    """The current selection as a URL token plus a copyable snippet — no
    embedded editor."""
    if not project:
        return _pick_project_first()
    selection = service.analysis_selection(project, tray)
    token = encode_selection_token(selection)
    snippet = python_snippet(token, project)
    return html.Div(
        [
            html.Section(
                [
                    html.H3("Selection token"),
                    html.P(
                        "The token the URL carries as ?sel=… — the same "
                        "format the jernerics client encodes, so it parses "
                        "on both sides.",
                        className="hint",
                    ),
                    html.Div(
                        [
                            html.Pre(token, className="config-json"),
                            dcc.Clipboard(content=token),
                        ],
                        className="snippet-row",
                    ),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Continue in Python"),
                    html.P(
                        "Point TrackingClient at your tracking server and go; "
                        "decode_selection returns the typed Selection.",
                        className="hint",
                    ),
                    html.Div(
                        [
                            html.Pre(snippet, className="config-json"),
                            dcc.Clipboard(content=snippet),
                        ],
                        className="snippet-row",
                    ),
                ],
                className="section",
            ),
        ]
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
    return {
        **doc,
        "series": {**doc["series"], "context_filters": filters},
    }


_SWATCH_COLUMN: dict[str, Any] = {
    "headerName": "",
    "field": "swatch",
    "cellRenderer": {"function": "jernericsColorSwatch(params)"},
    "maxWidth": 48,
    "minWidth": 48,
    "sortable": False,
    "filter": False,
    "resizable": False,
    "pinned": "left",
}


def browser_trial_columns(
    param_keys: Sequence[str], *, multi_sweep: bool
) -> list[dict[str, Any]]:
    """Persistent trial-browser columns: the trace color swatch, trial
    identity, sweep when the scope spans several, and one column per
    varying sampled parameter (horizontal scroll, never a truncation)."""
    return [
        dict(_SWATCH_COLUMN),
        {"headerName": "#", "field": "number", "maxWidth": 80},
        {"headerName": "Trial", "field": "trial_short"},
        *([{"headerName": "Sweep", "field": "sweep"}] if multi_sweep else []),
        {"headerName": "State", "field": "state"},
        {"headerName": "Objective", "field": "objective"},
        {"headerName": "Executions", "field": "executions"},
        {"headerName": "Generations", "field": "generations", "maxWidth": 120},
        *({"headerName": key, "field": f"p_{key}"} for key in param_keys),
    ]


def _browser_records(
    trials: list[dict[str, Any]], color: str | None, series_data: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """Records whose color grouping matches the chart's: the pooled
    series for a context choice (context lives on values), the scoped
    trials otherwise."""
    if color is not None and figures.parse_color(color)[0] == "context":
        return [
            series
            for entry in (series_data or {}).get("per_key", {}).values()
            for series in entry.get("series", [])
        ]
    return [
        {"trial": trial["trial_id"], "params": trial.get("params") or {}, "context": {}}
        for trial in trials
    ]


def browser_trial_outputs(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
    series_data: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(columns, rows, selected rows) for the persistent trial browser:
    one row per retry family with its current trial, the trace color
    swatch under the active color choice, execution short ids, and every
    varying sampled parameter."""
    if not project or service is None:
        return [], [], []
    trials = service.analysis_trials(project, tray)
    meta = {trial["trial_id"]: trial for trial in trials}
    param_keys = varying_param_keys(trials)
    families = service.analysis_families(project, (tray or {}).get("sweeps") or [])
    color = (view_doc or default_view_state())["series"]["color"]
    grouping = figures.color_grouping(
        _browser_records(trials, color, series_data), color
    )
    colors = grouping["colors"]
    series_by_trial = {
        series["trial"]: series
        for entry in (series_data or {}).get("per_key", {}).values()
        for series in entry.get("series", [])
    }
    rows = []
    for family in families:
        trial_id = family["current_trial"]
        trial = meta.get(trial_id) or {}
        record = series_by_trial.get(trial_id) or {
            "trial": trial_id,
            "params": trial.get("params") or {},
            "context": {},
        }
        rows.append(
            {
                "root": family["root"],
                "trial_id": trial_id,
                "number": family["number"],
                "trial_short": short_id(trial_id),
                "sweep": trial.get("sweep_name", trial.get("sweep_id", "")),
                "state": family["state"],
                "objective": components.objective_text(family["objective"]),
                "executions": family.get("executions") or "",
                "generations": family["generations"],
                "swatch": colors.get(figures.identity_of(record, grouping), "#7f7f7f"),
                **{
                    f"p_{key}": param_text((trial.get("params") or {}).get(key))
                    for key in param_keys
                },
            }
        )
    picked = set((tray or {}).get("families") or [])
    columns = browser_trial_columns(
        param_keys,
        multi_sweep=len({row["sweep"] for row in rows if row["sweep"]}) > 1,
    )
    return columns, rows, [row for row in rows if row["root"] in picked]


def view_from_trace_click(
    current: dict[str, Any] | None, click: dict[str, Any] | None
) -> dict[str, Any] | None:
    """View doc after a trace click: focus that trial (inspector opens,
    scope untouched) and highlight it alone — or clear the highlight when
    it was the only one. ``None`` when Plotly exposed no identity."""
    points = (click or {}).get("points") or []
    identity = points[0].get("customdata") if points else None
    if not identity:
        return None
    doc = current or default_view_state()
    picked = [str(identity)]
    if doc["highlighted_trials"] == picked:
        picked = []
    return {
        **doc,
        "highlighted_trials": picked,
        "focus": {"kind": "trial", "id": str(identity)},
    }


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


def auto_refresh_polls(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
) -> bool:
    """The poll interval runs only while the persisted auto-refresh
    intent is on AND the selected scope still has incomplete work."""
    if not (view_doc or {}).get("auto_refresh"):
        return False
    if service is None or not project:
        return False
    return service.analysis_scope_incomplete(project, tray)


def auto_refresh_flip(
    view_doc: dict[str, Any] | None, incomplete: bool
) -> dict[str, Any] | None:
    """Doc clearing auto-refresh once the scope turned terminal; ``None``
    keeps the persisted intent."""
    if not view_doc or not view_doc.get("auto_refresh") or incomplete:
        return None
    return {**view_doc, "auto_refresh": False}


def _extract_series_figure(panels: list[Any]) -> tuple[list[Any], Any]:
    """(panels without the embedded graph, its figure): the callback
    writes the figure to the layout-level graph, so Plotly's uirevision
    keeps user zoom across refreshes instead of losing a replaced div."""
    for index, node in enumerate(panels):
        if isinstance(node, dcc.Graph):
            return [*panels[:index], *panels[index + 1 :]], node.figure
    return panels, figures.empty_figure()


def series_tab_outputs(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
    now_ns: int,
) -> tuple[Any, ...]:
    """The series callback's outputs: panels/payload/options from the
    series read, the context-filter controls from the discovery read,
    the figure for the stable graph (a clientside merge re-applies the
    user's zoom), the status/updated facts, and the refresh-state store
    payload."""
    doc = view_doc or default_view_state()
    panels, payload, key_options, dim_options, _facet = series_outputs(
        service, project, tray, doc
    )
    panels, figure = _extract_series_figure(panels)
    dims = (
        service.analysis_context_values(project, tray)
        if project and service is not None
        else []
    )
    filters_ui = context_filter_controls(dims, doc["series"]["context_filters"])
    incomplete = (
        service.analysis_scope_incomplete(project, tray)
        if project and service is not None
        else False
    )
    return (
        panels,
        payload,
        key_options,
        dim_options,
        dim_options,
        filters_ui,
        series_status(doc, payload, incomplete=incomplete),
        updated_ago(now_ns),
        figure,
        {"error": "", "at_ns": now_ns},
    )


def refresh_failure(error: Exception, now_ns: int) -> tuple[Any, ...]:
    """The series callback's no_update tuple for a failed refresh: the
    last successful panels, figure, options, and controls survive and
    the error surfaces in the status and message regions."""
    message = f"refresh failed — keeping the last successful view: {error}"
    return (
        no_update,
        no_update,
        no_update,
        no_update,
        no_update,
        no_update,
        Error(message),
        no_update,
        no_update,
        {"error": message, "at_ns": now_ns},
    )
