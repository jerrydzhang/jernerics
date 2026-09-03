"""Analysis page: cross-sweep exploration and comparison (jernerics-h5d.13).

One project's sweeps, trials, and retry families feed a shared selection
scope. The scope lives in one canonical ``scope`` group of the view
document — sweep picks, family/trial/execution picks, the expansion
toggle, and the include flags — so the browser tray UI and the include
controls read and write the same state. That document round-trips
through the dashboard-only ``?view=<json>`` parameter as a defaults-diff
(jernerics-2se): only non-default fields are encoded, while legacy full
documents still decode. A ``?sel=<token>`` deep link — the same token
format the jernerics client uses, still the continue-in-Python handoff —
hydrates into the scope too. A persistent scope bar plus an Edit-scope
disclosure sit above every view (jernerics-cdf.3). Tabs: data catalog,
series overlay, points tables, study-style Optuna views (plain plotly —
no optuna dependency), and the continue-in-Python handoff. All data
flows through DashboardService.
"""

import json
import math
from collections.abc import Sequence
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from dash import dcc, html, no_update
from dash_ag_grid import AgGrid
from jernerics_schema import Selection, SelectionTokenError, encode_selection

from . import components, figures
from .components import (
    MISSING,
    Empty,
    Error,
    clamp_text,
    clamped_column,
    relative_time,
    short_id,
)
from .routes import ROUTES_BASE, parse_route
from .selection_tokens import decode_selection_token
from .service import ANALYSIS_REDUCTIONS, DashboardService

EMPTY_TRAY: dict[str, Any] = {
    "sweeps": [],
    "trials": [],
    "families": [],
    "executions": [],
    "expand": False,
}
"""Selection dimensions of the scope group: sweep ids, explicit trial
ids, picked retry-family roots, explicit execution ids, and the
per-family expansion toggle. The workspace sweep grid and the analysis
pickers all read and write these keys inside ``view.scope``."""

_TRAY_KEYS = tuple(EMPTY_TRAY)

VIEW_VERSION = 2
"""Wire version of the dashboard-only ``view=`` URL document."""

_LEGACY_VIEW_VERSION = 1
"""Version-1 documents carried the include flags at the top level and
no scope group; they still decode to the same effective state."""

_GRID_DEFAULTS: dict[str, Any] = {
    "sortable": True,
    "filter": True,
    "resizable": True,
    "minWidth": 100,
}
_ANALYSIS_VIEWS = ("overview", "investigations", "exceptions")
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


def default_scope_state() -> dict[str, Any]:
    """The scope group with every dimension empty and both include
    flags off."""
    return {**EMPTY_TRAY, "include_archived": False, "include_invalid": False}


def default_view_state() -> dict[str, Any]:
    """The v2 view document with every control at its default."""
    return {
        "v": VIEW_VERSION,
        "active": "overview",
        "auto_refresh": False,
        "overview_filter": None,
        "scope": default_scope_state(),
        "focus": None,
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


def _decode_scope(value: Any) -> dict[str, Any]:
    """A validated scope group; unknown fields are dropped and missing
    fields take defaults."""
    _require(isinstance(value, dict), "scope must be an object")
    scope = default_scope_state()
    for key in ("sweeps", "trials", "families", "executions"):
        if key not in value:
            continue
        ids = _string_list(value[key], f"scope.{key}")
        scope[key] = list(dict.fromkeys(ids)) if key == "sweeps" else ids
    for key in ("expand", "include_archived", "include_invalid"):
        if key not in value:
            continue
        flag = value[key]
        _require(isinstance(flag, bool), f"scope.{key} must be a boolean")
        scope[key] = flag
    return scope


def _legacy_view_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """A v1 full document rewritten into the v2 field layout: the
    top-level include flags move into the scope group, which v1 tokens
    never carried (selection lived in the session tray)."""
    fields = {
        key: value
        for key, value in payload.items()
        if key not in ("v", "include_archived", "include_invalid")
    }
    return {
        **fields,
        "v": VIEW_VERSION,
        "scope": {
            **default_scope_state(),
            "include_archived": payload.get("include_archived", False),
            "include_invalid": payload.get("include_invalid", False),
        },
    }


def decode_view_state(raw: str) -> dict[str, Any]:
    """The canonical view document from a ``view=`` value; unknown
    fields are dropped, missing fields take defaults, wrong types or
    enum values raise :class:`ViewStateError`. Version-2 tokens carry
    only non-default fields; a legacy full document (version 1) decodes
    to the same effective state."""
    try:
        payload = json.loads(raw)
    except ValueError as error:
        raise ViewStateError(f"view state is malformed: {error}") from error
    _require(isinstance(payload, dict), "view state must be a JSON object")
    version = payload.get("v")
    _require(
        isinstance(version, int) and not isinstance(version, bool),
        f"unsupported view state: expected version {VIEW_VERSION}",
    )
    if version == _LEGACY_VIEW_VERSION:
        payload = _legacy_view_payload(payload)
    else:
        _require(
            version == VIEW_VERSION,
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
    doc["overview_filter"] = _overview_filter(payload.get("overview_filter"))
    doc["scope"] = _decode_scope(payload.get("scope", {}))
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
    """Percent-encoded compact JSON for the ``view=`` URL parameter —
    a defaults-diff: the version marker plus only the fields that
    differ from :func:`default_view_state` (jernerics-2se)."""
    defaults = default_view_state()
    payload: dict[str, Any] = {"v": VIEW_VERSION}
    for key, value in doc.items():
        if key != "v" and value != defaults.get(key):
            payload[key] = value
    return quote(json.dumps(payload, separators=(",", ":"), sort_keys=True), safe="")


def _overview_filter(value: Any) -> str | None:
    """A validated operational-tile filter: the execution-health keys
    or a ``state:`` sweep-state key, else ``None``."""
    if value is None:
        return None
    _require(
        isinstance(value, str) and value != "",
        "overview_filter must be a non-empty string or null",
    )
    if value in ("failed", "stale"):
        return value
    _require(
        value.startswith("state:") and len(value) > len("state:"),
        f"unsupported overview filter {value!r}",
    )
    return value


def decode_focus(value: Any) -> dict[str, str] | None:
    """A validated focus object ``{kind, id}``; ``None`` when absent."""
    if value is None:
        return None
    _require(isinstance(value, dict), "focus must be an object")
    kind = value.get("kind")
    _require(kind in _FOCUS_KINDS, "focus.kind must be sweep, trial, or execution")
    focus_id = _optional_key(value.get("id"), "focus.id")
    _require(focus_id is not None, "focus.id must be a non-empty string")
    return {"kind": str(kind), "id": str(focus_id)}


def edited_view(
    current: dict[str, Any] | None, changes: dict[str, Any]
) -> dict[str, Any]:
    """The one door for view-doc writes: ``changes`` applied over
    ``current`` (defaults when absent); the current ``focus`` survives
    every edit except one that names it (jernerics-gk6)."""
    doc = {**(current or default_view_state()), **changes}
    if "focus" not in changes:
        doc["focus"] = (current or {}).get("focus")
    return doc


def with_focus(
    current: dict[str, Any] | None, focus: dict[str, str] | None
) -> dict[str, Any]:
    """View doc after a focus edit; nothing but ``focus`` changes — focus
    edits never narrow scope."""
    return edited_view(current, {"focus": focus})


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
    document yields defaults plus a visible error. The inspector focus
    survives both — only a parameter that names one, or an explicit
    focus edit, moves it (jernerics-gk6). The scope survives a
    parameter-less URL only while a ``?sel=`` token owns the hydration
    (its dimensions land over the current scope); a plain navigation
    resets it."""
    if parse_route(pathname).kind != "workspace":
        return None, None
    raw = _view_param(search)
    if raw is not None:
        try:
            decoded = decode_view_state(raw)
        except ViewStateError as error:
            changes = {
                key: value
                for key, value in default_view_state().items()
                if key != "focus"
            }
            return canonical_view(edited_view(current, changes)), str(error)
        changes = {key: value for key, value in decoded.items() if key != "focus"}
        if decoded["focus"] is not None:
            changes["focus"] = decoded["focus"]
    else:
        changes = {
            key: value
            for key, value in default_view_state().items()
            if key not in ("focus", "scope")
        }
        if _sel_param(search) is None:
            changes["scope"] = default_scope_state()
    doc = edited_view(current, changes)
    return (None if current == doc else canonical_view(doc)), None


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
    return edited_view(
        doc,
        {
            "active": (
                active
                if "active" in edited and active in _ANALYSIS_VIEWS
                else doc["active"]
            ),
            "series": series,
            "optuna": optuna,
            "auto_refresh": auto_refresh_state,
        },
    )


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


def _counted(count: int, singular: str, plural: str) -> str:
    """``count`` with the noun form that matches it."""
    return f"{count} {singular if count == 1 else plural}"


def tray_summary(tray: dict[str, Any] | None) -> str:
    """Header line for the selection tray; empty when nothing is selected."""
    tray = tray or EMPTY_TRAY
    sweeps = len(tray.get("sweeps") or [])
    trials = len(tray.get("trials") or [])
    families = len(tray.get("families") or [])
    executions = len(tray.get("executions") or [])
    if not (sweeps or trials or families or executions):
        return ""
    parts = [
        _counted(sweeps, "sweep", "sweeps"),
        _counted(trials, "trial", "trials"),
        _counted(families, "family", "families"),
    ]
    if executions:
        parts.append(_counted(executions, "execution", "executions"))
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
    """Merge grid/expand edits into the scope group; explicit
    trials/executions (kept from a hydrated token) and the include
    flags survive edits.

    Only the control the event actually carried is authoritative for its
    dimension — every other dimension keeps the current scope. A grid
    event fires while the OTHER grid may still hold a stale selection
    snapshot (AG Grid applies programmatic selectedRows per grid, not
    atomically), and a mount echo of the pre-hydration state must not
    erase the dimensions the user did not touch (jernerics-8c9)."""
    current = current or default_scope_state()
    return {
        **current,
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


def _canonical_ids(values: Any) -> list[str]:
    """Id strings in the sorted-unique form the browser grids echo back."""
    return sorted({str(value) for value in values or ()})


def tray_from_selection(selection: Any) -> dict[str, Any]:
    """Scope selection dimensions matching a decoded token selection.

    Retry roots hydrate as picked families with the expansion toggle on:
    that is exactly what a retry-root selection means, and it keeps the
    hydrated scope's effective selection equal to the decoded one. The
    include flags are not token facts — the caller merges these
    dimensions over the current scope group.
    """
    return {
        "sweeps": _canonical_ids(selection.sweeps),
        "trials": [str(value) for value in selection.trials or ()],
        "families": _canonical_ids(selection.retry_roots),
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
    """(scope dimensions, error) for a URL carrying ``?sel=``. A
    ``None`` scope means "leave the current scope alone" (no token, a
    different page, or a token equal to what is already shown). A token
    scoped to another project surfaces as an error instead of mixing.
    With no project picked, the token only decides the cold start
    (jernerics-xbx): the shell adopts the token's project through the
    picker and hydration re-fires when project-store settles, while a
    token the dashboard cannot act on surfaces its error instead of
    silently empty grids."""
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
    token_tray = tray_from_selection(selection)
    if current and service.analysis_selection(
        project, current
    ) == service.analysis_selection(project, token_tray):
        return None, None
    return token_tray, None


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


def expand_values(doc: dict[str, Any] | None) -> list[str]:
    """Expansion-toggle checklist values matching the view scope."""
    scope = (doc or default_view_state()).get("scope") or default_scope_state()
    return ["expand"] if scope.get("expand") else []


def include_values(doc: dict[str, Any] | None) -> list[str]:
    """Include-control checklist values matching the view scope."""
    scope = (doc or default_view_state()).get("scope") or default_scope_state()
    values = []
    if scope.get("include_archived"):
        values.append("archived")
    if scope.get("include_invalid"):
        values.append("invalid")
    return values


def view_from_include(
    current: dict[str, Any] | None, values: list[str] | None
) -> dict[str, Any]:
    """View state after an include-control edit; only the two include
    flags change — the picked dimensions survive."""
    doc = current or default_view_state()
    picked = set(values or [])
    return edited_view(
        current,
        {
            "scope": {
                **doc.get("scope", default_scope_state()),
                "include_archived": "archived" in picked,
                "include_invalid": "invalid" in picked,
            }
        },
    )


def canonical_view(doc: dict[str, Any]) -> dict[str, Any]:
    """The doc exactly as the include-control echo would rewrite it, so
    a hydrated view store leaves the echo nothing to change."""
    return view_from_include(doc, include_values(doc))


def synced_search(
    pathname: str | None,
    view_doc: dict[str, Any] | None,
    current_search: str | None,
    *,
    url_navigated: bool,
) -> str | None:
    """The URL search after a navigation or a view edit; ``None`` leaves
    it alone. Navigations may only drop the workspace parameters —
    minting on navigation would let a stale document clobber a freshly
    opened deep link before hydration lands, and only the editor page
    keeps its own query (its ``?sweeps=`` seed). View edits mint, and
    only on the workspace page; the scope rides the document, so no
    separate ``?sel=`` is minted anymore."""
    if url_navigated:
        kind = parse_route(pathname).kind
        if current_search and kind not in ("workspace", "investigation-edit"):
            return ""
        return None
    if parse_route(pathname).kind != "workspace":
        return None
    return search_from_state(view_doc, current_search)


def view_query(view_doc: dict[str, Any] | None) -> str:
    """The ``view=`` query fragment; empty when the state is absent or
    default (a default state does not belong in the URL)."""
    if not view_doc or view_doc == default_view_state():
        return ""
    return f"view={encode_view_state(view_doc)}"


def _query_search(fragments: list[str]) -> str:
    joined = "&".join(fragment for fragment in fragments if fragment)
    return f"?{joined}" if joined else ""


def search_from_state(
    view_doc: dict[str, Any] | None,
    current_search: str | None,
) -> str | None:
    """URL search carrying the ``view=`` parameter; ``None`` when
    unchanged. A current ``view=`` that does not decode is left in place
    (the visible error stays until a real edit rewrites it)."""
    target = _query_search([view_query(view_doc)])
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
    context = service.analysis_context_catalog(project, tray)
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


def scope_dims(scope: dict[str, Any] | None) -> dict[str, Any]:
    """The selection dimensions of a scope group, include flags
    excluded — exactly what a typed Selection read consumes."""
    scope = scope or {}
    return {key: scope.get(key) for key in _TRAY_KEYS}


def scope_fingerprint(project: str | None, tray: dict[str, Any] | None) -> str:
    """Canonical identity of the (project, scope) a snapshot serves; the
    include flags are discovery-only and never change analysis reads."""
    return json.dumps(
        {"project": project or "", "scope": scope_dims(tray)},
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


def series_outputs(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
    view_doc: dict[str, Any] | None,
) -> tuple[
    list[Any], dict, list[dict[str, str]], list[dict[str, Any]], list[dict[str, str]]
]:
    """One-shot composition: build the snapshot, then render it. The
    callbacks keep the snapshot in the store and render from it; this
    serves fresh one-shot renders."""
    snapshot = series_snapshot(service, project, tray, view_doc, 0)
    panels, _payload, key_options, color_options, facet_options, *_rest = (
        render_series_outputs(view_doc, snapshot)
    )
    return panels, snapshot, key_options, color_options, facet_options


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


def _format_payload(payload: Any) -> str:
    if isinstance(payload, dict | list):
        return json.dumps(payload, indent=2, sort_keys=True)
    if payload is None:
        return MISSING
    if isinstance(payload, bool):
        return "true" if payload else "false"
    return str(payload)


def _key_header(key: str, detail: str) -> dict[str, str]:
    """Header text for one possibly long key: clamped label, full key
    in the header tooltip (jernerics-l8f)."""
    return {
        "headerName": f"{clamp_text(key)} · {detail}",
        "headerTooltip": f"{key} · {detail}",
    }


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
                **_key_header(
                    entry["key"], f"{entry['kind']} · {present}/{len(labels)}"
                ),
                "field": entry["key"],
                **clamped_column(),
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
                **_key_header(key, f"{present}/{len(labels)}"),
                "field": key,
                **clamped_column(),
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
                    html.P(
                        "Long values clamp; click a clamped cell to open the full "
                        "payload.",
                        className="hint",
                    ),
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
                    html.P(
                        "Long values clamp; click a clamped cell to open the full "
                        "value.",
                        className="hint",
                    ),
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
                                ],
                                className="figure-wide",
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
                                ],
                                className="figure-wide",
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


def origin_from_href(href: str | None) -> str:
    """The browser origin (``scheme://netloc``) a snippet points at."""
    parts = urlparse(href or "")
    if not parts.netloc:
        return "http://localhost:8000"
    return f"{parts.scheme or 'http'}://{parts.netloc}"


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


def python_tab(
    service: DashboardService,
    project: str | None,
    tray: dict[str, Any] | None,
    base_url: str,
) -> html.Div:
    """The current selection as a URL token plus a copyable snippet — no
    embedded editor."""
    if not project:
        return _pick_project_first()
    selection = service.analysis_selection(project, tray)
    token = encode_selection(selection)
    snippet = python_snippet(token, project, base_url)
    pre_style = {"whiteSpace": "pre", "overflowX": "auto"}
    return html.Div(
        [
            html.Section(
                [
                    html.H3("Selection token"),
                    html.P(
                        "The token the URL carries as ?sel=… — minted by "
                        "the shared jernerics-schema codec, so the client "
                        "and dashboard parse the same token.",
                        className="hint",
                    ),
                    html.Div(
                        [
                            html.Pre(token, className="config-json", style=pre_style),
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
                            html.Pre(snippet, className="config-json", style=pre_style),
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
    return edited_view(
        doc,
        {
            "highlighted_trials": picked,
            "focus": {"kind": "trial", "id": str(identity)},
        },
    )


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
    view_doc: dict[str, Any] | None,
) -> bool:
    """The poll interval runs only while the persisted auto-refresh
    intent is on AND the selected scope still has incomplete work."""
    if not (view_doc or {}).get("auto_refresh"):
        return False
    if service is None or not project:
        return False
    return service.analysis_scope_incomplete(project, (view_doc or {}).get("scope"))


def auto_refresh_flip(
    view_doc: dict[str, Any] | None, incomplete: bool
) -> dict[str, Any] | None:
    """Doc clearing auto-refresh once the scope turned terminal; ``None``
    keeps the persisted intent."""
    if not view_doc or not view_doc.get("auto_refresh") or incomplete:
        return None
    return edited_view(view_doc, {"auto_refresh": False})


def _extract_series_figure(panels: list[Any]) -> tuple[list[Any], Any]:
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
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """The data callback's outputs: the fresh canonical snapshot, the
    updated-ago line, and the refresh state (no error)."""
    return (
        series_snapshot(service, project, tray, view_doc, now_ns),
        updated_ago(now_ns),
        {"error": "", "at_ns": now_ns},
    )


def series_data_failure(error: Exception, now_ns: int) -> tuple[Any, ...]:
    """The data callback's no_update tuple for a failed refresh: the
    last successful snapshot survives and the error surfaces in the
    refresh-state store."""
    message = f"refresh failed — keeping the last successful view: {error}"
    return no_update, no_update, {"error": message, "at_ns": now_ns}


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
    panels, figure = _extract_series_figure(panels)
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
    successful presentation survives and the error reaches the message
    region through the refresh-state store."""
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
