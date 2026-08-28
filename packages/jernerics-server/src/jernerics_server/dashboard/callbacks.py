"""Navigation, project picker, selection tray, and polling callbacks.

Routing model after the workspace cutover: the project catalog and the
persistent workspace are the only non-artifact pages; sweeps, trials,
and executions render in the workspace's inspector region, driven by
the view document's ``focus`` field.
"""

import ast
import json
import time
from typing import Any, Literal
from uuid import UUID

import dash
from dash import Input, Output, State, no_update
from dash.exceptions import PreventUpdate

from . import analysis, artifacts, components, layout, workspace
from .components import Error
from .routes import ROUTES_BASE, parse_route
from .service import (
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
)


def pattern_trigger(context: Any) -> tuple[str | None, str]:
    """(metric, control) of the pattern component that fired a MATCH
    callback; ``(None, "")`` when nothing did. Pattern prop ids carry
    the resolved id as a JSON object before the final ``.prop``."""
    triggered = context.triggered
    if not triggered:
        return None, ""
    id_json = triggered[0]["prop_id"].rsplit(".", 1)[0]
    try:
        resolved = json.loads(id_json)
    except ValueError:
        return None, ""
    if not isinstance(resolved, dict) or len(resolved) != 1:
        return None, ""
    control, metric = next(iter(resolved.items()))
    return str(metric), str(control)


def overlay_axis_control(triggered: Any) -> str | None:
    """The ``analysis-overlay-*`` control field that fired, if any."""
    for item in triggered or []:
        prop_id = str(item.get("prop_id", ""))
        if prop_id.startswith("analysis-overlay-"):
            return prop_id.rsplit(".", 1)[0].removeprefix("analysis-overlay-")
    return None


def pattern_input_value(
    inputs_list: Any, entry: int, pattern_key: str, metric: str
) -> Any:
    """The current value of one ALL-pattern input, located by its
    resolved component id (ALL inputs arrive as lists)."""
    items = inputs_list[entry] if isinstance(inputs_list, list) else []
    for item in items or []:
        component_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(component_id, dict) and component_id.get(pattern_key) == metric:
            return item.get("value")
    return None


def page_content(
    pathname: str | None,
    service: DashboardService,
    *,
    now_ns: int | None = None,
    workspace_state_doc: dict | None = None,
    view_doc: dict | None = None,
) -> tuple[Any, bool]:
    """(page, poll enabled) for a URL, with live data.

    ``poll enabled`` is True only while the workspace's work is
    incomplete: any sweep still running or the focused object open.
    """
    spec = parse_route(pathname)
    now = time.time_ns() if now_ns is None else now_ns
    if spec.kind == "project":
        return layout.project_page(service.project_catalog(), now), False
    if spec.kind == "workspace":
        project = spec.object_id or ""
        summaries = service.sweep_overview(project)
        polls = any(summary.incomplete for summary in summaries) or (
            workspace.focus_incomplete(service, (view_doc or {}).get("focus"))
        )
        state = workspace_state(workspace_state_doc, project)
        return (
            workspace.workspace_page(
                project,
                sort=state["sort"],
                quick=state["quick"],
                filters=state["filters"],
            ),
            polls,
        )
    if spec.kind == "artifact":
        view = service.artifact_view(spec.object_id or "")
        if view is None:
            return (
                layout.missing_object_page("artifact", spec.object_id or ""),
                False,
            )
        return artifacts.viewer_page(service, view, now), False
    return layout.not_found_page(pathname or ""), False


def is_initial() -> bool:
    """True inside a callback's initial call (nothing changed to fire
    it) — ``callback_context.triggered`` is falsy exactly then."""
    return not dash.callback_context.triggered


def project_options(projects: list[str]) -> list[dict[str, str]]:
    return [{"label": project, "value": project} for project in projects]


def tray_from_grid(rows: list[dict] | None, current: dict | None) -> dict:
    """Merge the browser's sweep checkbox selection into the unified
    selection store, keeping the active project and analysis-side picks
    so the tray survives focus edits."""
    return {
        **analysis.EMPTY_TRAY,
        **(current or {}),
        "sweeps": sorted({str(row["sweep_id"]) for row in rows or []}),
    }


def lineage_panel(rows: list[dict] | None, data: dict | None) -> list[object]:
    """Side-panel lineage for the family selected in the inspector grid."""
    lineage = (data or {}).get("lineage") or []
    root = str(rows[0]["root"]) if rows else None
    return workspace.lineage_chain(root, lineage)


def workspace_state(store: dict | None, project: str | None) -> dict:
    """Per-project browser controls state from the session store."""
    saved = (store or {}).get(project or "") or {}
    return {
        "quick": str(saved.get("quick") or ""),
        "filters": saved.get("filters") or None,
        "sort": saved.get("sort") or None,
    }


def sort_from_columns(columns: list | None) -> list | None:
    """Sort entries (colId/sort) extracted from AG Grid column state."""
    entries = [
        {"colId": column["colId"], "sort": column["sort"]}
        for column in columns or []
        if isinstance(column, dict) and column.get("sort")
    ]
    return entries or None


def remember_workspace(
    current: dict | None, project: str | None, **fields: object
) -> dict | None:
    """Session store after one browser control edit; ``None`` when the
    per-project state is unchanged (only edited fields are
    authoritative, so a mounting control's echo cannot wipe the rest)."""
    state = workspace_state(current, project)
    updated = {**state, **fields}
    if updated == state:
        return None
    return {**(current or {}), project or "": updated}


_CURATION_VERBS = {
    "archive": "Archived",
    "invalid": "Marked invalid",
    "restore_validity": "Restored validity of",
    "restore": "Restored",
}


def run_curation(
    service: DashboardService, action: str, sweep_id: str, reason: str
) -> str:
    """One service mutation per action name; returns the sweep's label."""
    if action == "archive":
        return service.archive_sweep(sweep_id)
    if action == "invalid":
        return service.mark_sweep_invalid(sweep_id, reason)
    if action == "restore_validity":
        return service.restore_sweep_validity(sweep_id)
    return service.restore_sweep(sweep_id)


def apply_curation(
    service: DashboardService, action: str, sweep_ids: list[str], reason: str = ""
) -> tuple[bool, str]:
    """Run one curation action over ids deterministically; (all ok,
    report) with per-sweep failures named, never a false all-succeeded."""
    if action == "invalid" and not reason.strip():
        return (
            False,
            "Mark invalid requires a reason (1..500 characters after trimming).",
        )
    labels: list[str] = []
    failures: list[str] = []
    for sweep_id in sorted(set(sweep_ids)):
        try:
            labels.append(run_curation(service, action, sweep_id, reason))
        except (CurationUnavailableError, CurationRejectedError) as error:
            failures.append(f"{service.sweep_label(sweep_id)}: {error}")
    verb = _CURATION_VERBS[action]
    report = f"{verb} {', '.join(labels)}." if labels else ""
    if failures:
        prefix = f"{report} " if report else ""
        report = f"{prefix}Failed — {'; '.join(failures)}."
    return not failures, report


def triggered_action(triggered: set[str], mapping: dict[str, str]) -> str | None:
    """The action name for the one triggered control, if any."""
    return next((action for prop, action in mapping.items() if prop in triggered), None)


def focus_from_trigger(triggered: Any) -> dict[str, str] | Literal[""] | None:
    """The focus a row-click or button event names: ``{kind, id}``,
    ``None`` to clear, or ``""`` when nothing focusable fired."""
    for prop_id in triggered or ():
        text = str(prop_id)
        if text.startswith("inspector-close."):
            return None
        if text.startswith(("sweep-grid.", "analysis-family-grid.")) or (
            '"focus-family"' in text
        ):
            continue
        if ".cellClicked" not in text and ".n_clicks" not in text:
            continue
        head, _, _prop = text.rpartition(".")
        try:
            ident = ast.literal_eval(head)
        except ValueError:
            continue
        if isinstance(ident, dict) and "focus-object" in ident:
            kind, _, object_id = str(ident["focus-object"]).partition(":")
            return {"kind": kind, "id": object_id}
    return ""


def register_callbacks(app: dash.Dash, service: DashboardService) -> None:
    @app.callback(
        Output("page-container", "children"),
        Output("poll", "disabled"),
        Output("view-store", "data", allow_duplicate=True),
        Output("route-store", "data"),
        Input("url", "pathname"),
        Input("poll", "n_intervals"),
        State("workspace-store", "data"),
        State("view-store", "data"),
        State("route-store", "data"),
        prevent_initial_call="initial_duplicate",
    )
    def _render_page(
        pathname: str | None,
        _tick: int | None,
        workspace_doc: dict | None,
        view_doc: dict | None,
        rendered_route: str | None,
    ):
        kind = parse_route(pathname).kind
        project = parse_route(pathname).object_id
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        ticked = "poll.n_intervals" in triggered
        # A url.search rewrite re-fires the pathname watcher with the
        # route unchanged; re-rendering would remount the workspace and
        # orphan every grid under it for nothing.
        if (
            not ticked
            and set(triggered) == {"url.pathname"}
            and (pathname == rendered_route)
        ):
            raise PreventUpdate
        # A tick never re-mounts anything: the workspace's own data
        # callbacks re-query on the same interval, so only the poll gate
        # and the auto-refresh flip re-evaluate here.
        if ticked and kind == "workspace":
            polls = any(
                summary.incomplete for summary in service.sweep_overview(project or "")
            ) or workspace.focus_incomplete(service, (view_doc or {}).get("focus"))
            flip = analysis.auto_refresh_flip(view_doc, polls)
            return (
                no_update,
                not polls,
                no_update if flip is None else flip,
                no_update,
            )
        page, polls = page_content(
            pathname,
            service,
            workspace_state_doc=workspace_state(workspace_doc, project),
            view_doc=view_doc,
        )
        return page, not polls, no_update, pathname

    @app.callback(
        Output("poll", "disabled", allow_duplicate=True),
        Output("poll-gate-facts-store", "data"),
        Input("url", "pathname"),
        Input("view-store", "data"),
        Input("selection-store", "data"),
        State("project-store", "data"),
        State("poll-gate-facts-store", "data"),
        prevent_initial_call=True,
    )
    def _gate_workspace_poll(
        pathname: str | None,
        view_doc: dict | None,
        tray: dict | None,
        project: str | None,
        facts: dict | None,
    ):
        # The router only fires on navigation and ticks; view and tray
        # edits must re-evaluate the interval themselves — but only when
        # a fact the gate consumes actually changed, never on every
        # view-store write.
        doc = view_doc or analysis.default_view_state()
        desired = {
            "project": project,
            "tray": tray,
            "auto_refresh": doc.get("auto_refresh"),
            "focus": doc.get("focus"),
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if (
            "url.pathname" not in triggered
            and (facts or {}).get("project") == desired["project"]
            and (facts or {}).get("tray") == desired["tray"]
            and (facts or {}).get("auto_refresh") == desired["auto_refresh"]
            and (facts or {}).get("focus") == desired["focus"]
        ):
            raise PreventUpdate
        if parse_route(pathname).kind != "workspace":
            raise PreventUpdate
        scope_open = bool(project) and service.analysis_scope_incomplete(project, tray)
        focus_open = workspace.focus_incomplete(service, doc.get("focus"))
        return (
            not (
                analysis.auto_refresh_polls(service, project, tray, view_doc)
                or scope_open
                or focus_open
            ),
            desired,
        )

    app.clientside_callback(
        """
        function(active) {
            const display = (value) => ({display: active === value ? "block" : "none"});
            return [display("overview"), display("catalog"), display("series"),
                    display("points"), display("optuna"), display("python")];
        }
        """,
        Output("workspace-overview", "style"),
        Output("analysis-catalog", "style"),
        Output("analysis-series-tab", "style"),
        Output("analysis-points", "style"),
        Output("analysis-optuna-tab", "style"),
        Output("analysis-python", "style"),
        Input("analysis-tabs", "value"),
    )

    @app.callback(
        Output("project-picker", "options"),
        Input("url", "pathname"),
    )
    def _load_projects(pathname: str | None):
        if pathname is None:
            raise PreventUpdate
        return project_options(service.projects())

    @app.callback(
        Output("url", "pathname"),
        Input("project-picker", "value"),
        prevent_initial_call=True,
    )
    def _navigate_to_workspace(project: str | None):
        target = f"{ROUTES_BASE}/project/{project}" if project else f"{ROUTES_BASE}/"
        return target

    @app.callback(
        Output("project-picker", "value"),
        Input("project-store", "data"),
        Input("url", "search"),
        State("project-picker", "value"),
    )
    def _show_current_project(
        project: str | None, search: str | None, picked: str | None
    ):
        # The picker mirrors project-store; with nothing picked, a
        # shared ?sel= token names the project for a fresh session
        # (jernerics-xbx). Picking it here — a plain write through the
        # picker's own callback — runs the exact settle path of a
        # manual pick, so the label follows and a chosen project is
        # never overridden. A view-only URL edit must not re-run that
        # settle path and cascade through the project store.
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if project:
            if "project-store.data" in triggered:
                # An identical mirror write re-fires the picker's own
                # watchers and re-enters the store; skip it.
                if project == picked:
                    raise PreventUpdate
                return project
            raise PreventUpdate
        selection, _error = analysis.cold_start(service, search)
        return selection.project if selection else None

    @app.callback(
        Output("project-store", "data"),
        Input("project-picker", "value"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _remember_project(project: str | None, current: str | None):
        if project == current:
            raise PreventUpdate
        return project

    @app.callback(
        Output("project-store", "data", allow_duplicate=True),
        Input("url", "pathname"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _adopt_project_from_url(pathname: str | None, current: str | None):
        spec = parse_route(pathname)
        if spec.kind == "workspace":
            if spec.object_id == current:
                raise PreventUpdate
            return spec.object_id
        raise PreventUpdate

    @app.callback(
        Output("selection-store", "data", allow_duplicate=True),
        Input("sweep-grid", "selectedRows"),
        State("selection-store", "data"),
        prevent_initial_call=True,
    )
    def _select_sweeps(rows: list[dict] | None, current: dict | None):
        tray = tray_from_grid(rows, current)
        if tray == (current or {}):
            raise PreventUpdate
        return tray

    @app.callback(
        Output("selection-tray", "children"),
        Input("selection-store", "data"),
        Input("project-store", "data"),
    )
    def _update_tray(tray: dict | None, _project: str | None):
        # The header summary is the one-click door into the scope
        # browser — it opens the browser, never a separate page.
        return analysis.tray_summary(tray)

    @app.callback(
        Output("scope-browser", "open"),
        Input("selection-tray", "n_clicks"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _open_scope_browser(_clicks: int | None, pathname: str | None):
        # The tray lives in the shell; the browser only exists on the
        # workspace page, so off-workspace clicks must not dispatch.
        if parse_route(pathname).kind != "workspace":
            raise PreventUpdate
        return True

    @app.callback(
        Output("family-lineage-panel", "children"),
        Input({"focus-family": dash.ALL}, "selectedRows"),
        State("family-lineage-store", "data"),
        prevent_initial_call=True,
    )
    def _show_lineage(rows: list, data: dict | None):
        picked = next((entry for entry in reversed(rows) if entry is not None), None)
        return lineage_panel(picked, data)

    # -- Scope browser and workspace curation ----------------------------

    @app.callback(
        Output("workspace-store", "data"),
        Input("workspace-quick", "value"),
        Input("sweep-grid", "filterModel"),
        Input("sweep-grid", "columnState"),
        State("project-store", "data"),
        State("workspace-store", "data"),
        prevent_initial_call=True,
    )
    def _remember_workspace(
        quick: str | None,
        filters: dict | None,
        columns: list | None,
        project: str | None,
        current: dict | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        fields: dict[str, object] = {}
        if "workspace-quick.value" in triggered:
            fields["quick"] = quick or ""
        if "sweep-grid.filterModel" in triggered:
            fields["filters"] = filters or None
        if "sweep-grid.columnState" in triggered:
            fields["sort"] = sort_from_columns(columns)
        updated = remember_workspace(current, project, **fields)
        if updated is None:
            raise PreventUpdate
        return updated

    @app.callback(
        Output("sweep-grid", "rowData"),
        Output("sweep-grid", "selectedRows"),
        Output("workspace-curation-note", "children"),
        Output("sweep-browser-facts-store", "data"),
        Input("project-store", "data"),
        Input("selection-store", "data"),
        Input("view-store", "data"),
        Input("poll", "n_intervals"),
        State("sweep-browser-facts-store", "data"),
    )
    def _load_browser_sweeps(
        project: str | None,
        tray: dict | None,
        view_doc: dict | None,
        _tick: int | None,
        facts: dict | None,
    ):
        # Scope data runs only for project/scope/include/refresh
        # changes; every other view-store write re-renders nothing here.
        desired = {
            "project": project,
            "sweeps": sorted(str(s) for s in (tray or {}).get("sweeps") or []),
            "include_archived": bool((view_doc or {}).get("include_archived")),
            "include_invalid": bool((view_doc or {}).get("include_invalid")),
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if "view-store.data" in triggered and (
            desired["include_archived"] == (facts or {}).get("include_archived")
            and desired["include_invalid"] == (facts or {}).get("include_invalid")
        ):
            raise PreventUpdate
        if not project:
            return [], analysis.mounted_selection([], initial=is_initial()), "", desired
        rows = workspace.browser_sweep_rows(
            service.sweep_overview(project),
            tray,
            include_archived=desired["include_archived"],
            include_invalid=desired["include_invalid"],
        )
        picked = set(desired["sweeps"])
        return (
            rows,
            analysis.mounted_selection(
                [row for row in rows if row["sweep_id"] in picked],
                initial=is_initial(),
            ),
            workspace.curation_note(rows),
            desired,
        )

    @app.callback(
        Output("analysis-family-grid", "columnDefs"),
        Output("analysis-family-grid", "rowData"),
        Output("analysis-family-grid", "selectedRows"),
        Output("trial-browser-facts-store", "data"),
        Input("selection-store", "data"),
        Input("project-store", "data"),
        Input("view-store", "data"),
        Input("poll", "n_intervals"),
        State("analysis-series-data", "data"),
        State("trial-browser-facts-store", "data"),
    )
    def _load_browser_families(
        tray: dict | None,
        project: str | None,
        view_doc: dict | None,
        _tick: int | None,
        series_data: dict | None,
        facts: dict | None,
    ):
        # The trial browser consumes the scope, the color choice, and
        # the series payload; any other view edit leaves it untouched.
        desired = {
            "tray": tray,
            "color": (view_doc or analysis.default_view_state())["series"]["color"],
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if "view-store.data" in triggered and desired == facts:
            raise PreventUpdate
        columns, rows, selected = analysis.browser_trial_outputs(
            service, project, tray, view_doc, series_data
        )
        return (
            columns,
            rows,
            analysis.mounted_selection(selected, initial=is_initial()),
            desired,
        )

    @app.callback(
        Output("analysis-scope-bar", "children"),
        Input("selection-store", "data"),
        Input("project-store", "data"),
    )
    def _render_scope_bar(tray: dict | None, project: str | None):
        return workspace.scope_bar(service, project, tray)

    @app.callback(
        Output("sweep-grid", "dashGridOptions"),
        Input("workspace-quick", "value"),
        prevent_initial_call=True,
    )
    def _filter_sweep_rows(text: str | None):
        return components.grid_options(
            rowSelection={"mode": "multiRow"}, quickFilterText=text or ""
        )

    @app.callback(
        Output("ws-archive", "disabled"),
        Output("ws-invalid", "disabled"),
        Output("ws-restore-validity", "disabled"),
        Output("ws-restore", "disabled"),
        Input("sweep-grid", "selectedRows"),
        prevent_initial_call=True,
    )
    def _offer_workspace_transitions(rows: list[dict] | None):
        offered = workspace.selection_transitions(rows)
        return (
            not offered["archive"],
            not offered["invalid"],
            not offered["restore_validity"],
            not offered["restore"],
        )

    WORKSPACE_ACTIONS = {
        "ws-archive.n_clicks": "archive",
        "ws-invalid.n_clicks": "invalid",
        "ws-restore-validity.n_clicks": "restore_validity",
        "ws-restore.n_clicks": "restore",
    }

    @app.callback(
        Output("workspace-message", "children"),
        Output("sweep-grid", "rowData", allow_duplicate=True),
        Output("sweep-grid", "selectedRows"),
        Output("workspace-curation-note", "children", allow_duplicate=True),
        Input("ws-archive", "n_clicks"),
        Input("ws-invalid", "n_clicks"),
        Input("ws-restore-validity", "n_clicks"),
        Input("ws-restore", "n_clicks"),
        State("sweep-grid", "selectedRows"),
        State("ws-reason", "value"),
        State("project-store", "data"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _curate_from_workspace(
        _archive: int,
        _invalid: int,
        _validity: int,
        _restore: int,
        rows: list[dict] | None,
        reason: str | None,
        project: str | None,
        view_doc: dict | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        action = triggered_action(triggered, WORKSPACE_ACTIONS)
        if action is None:
            raise PreventUpdate
        sweep_ids = [str(row["sweep_id"]) for row in rows or []]
        if not sweep_ids:
            return (
                workspace.action_message(
                    False, "Select sweeps first — actions apply to selected rows."
                ),
                no_update,
                no_update,
                no_update,
            )
        ok, report = apply_curation(service, action, sweep_ids, reason or "")
        fresh = workspace.browser_sweep_rows(
            service.sweep_overview(project or ""),
            None,
            include_archived=bool((view_doc or {}).get("include_archived")),
            include_invalid=bool((view_doc or {}).get("include_invalid")),
        )
        # Replacing rowData drops the grid's selection silently; writing
        # [] makes the drop an event, so tray and action-bar callbacks
        # re-fire against the post-action state.
        return (
            workspace.action_message(ok, report),
            fresh,
            [],
            workspace.curation_note(fresh),
        )

    DETAIL_ACTIONS = {
        "detail-archive.n_clicks": "archive",
        "detail-invalid.n_clicks": "invalid",
        "detail-restore-validity.n_clicks": "restore_validity",
        "detail-restore.n_clicks": "restore",
    }

    @app.callback(
        Output("detail-message", "children"),
        Output("detail-curation", "children"),
        Input("detail-archive", "n_clicks"),
        Input("detail-invalid", "n_clicks"),
        Input("detail-restore-validity", "n_clicks"),
        Input("detail-restore", "n_clicks"),
        State("view-store", "data"),
        State("detail-reason", "value"),
        prevent_initial_call=True,
    )
    def _curate_from_detail(
        _archive: int,
        _invalid: int,
        _validity: int,
        _restore: int,
        view_doc: dict | None,
        reason: str | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        action = triggered_action(triggered, DETAIL_ACTIONS)
        focus = (view_doc or {}).get("focus") or {}
        if action is None or focus.get("kind") != "sweep":
            raise PreventUpdate
        sweep_id = str(focus.get("id") or "")
        ok, report = apply_curation(service, action, [sweep_id], reason or "")
        detail = service.sweep_detail(sweep_id)
        banner = (
            workspace.detail_curation(detail.overview)
            if detail is not None
            else no_update
        )
        return workspace.action_message(ok, report), banner

    # -- Focus: the inspector region --------------------------------------

    @app.callback(
        Output("inspector", "children"),
        Output("inspector-render-store", "data"),
        Input("view-store", "data"),
        Input("poll", "n_intervals"),
        State("project-store", "data"),
        State("inspector-render-store", "data"),
    )
    def _render_inspector(
        view_doc: dict | None,
        _tick: int | None,
        _project: str,
        rendered: dict | None,
    ):
        # The inspector runs when the focus changes or the focused
        # object's live facts poll — never on unrelated view edits.
        focus = (view_doc or {}).get("focus")
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if (
            "poll.n_intervals" not in triggered
            and (rendered or {}).get("focus") == focus
        ):
            raise PreventUpdate
        return (
            workspace.inspector_content(service, focus, time.time_ns()),
            {"focus": focus},
        )

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"focus-object": dash.ALL}, "n_clicks"),
        Input("inspector-close", "n_clicks"),
        Input("sweep-grid", "cellClicked"),
        Input({"focus-family": dash.ALL}, "cellClicked"),
        Input("analysis-family-grid", "cellClicked"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_focus(
        _buttons: Any,
        _close: int | None,
        sweep_click: dict | None,
        family_clicks: list,
        browser_family_click: dict | None,
        current: dict | None,
    ):
        triggered = dash.callback_context.triggered_prop_ids
        fired = str(next(iter(triggered), ""))
        click = None
        kind = None
        if fired.startswith("sweep-grid."):
            click, kind = sweep_click, "sweep"
        elif '"focus-family"' in fired:
            click = next((c for c in reversed(family_clicks) if c), None)
            kind = "trial"
        elif fired.startswith("analysis-family-grid."):
            click, kind = browser_family_click, "trial"
        if click is not None and kind is not None:
            row_id = str((click or {}).get("rowId") or "")
            if row_id:
                doc = analysis.with_focus(current, {"kind": kind, "id": row_id})
                if doc == (current or {}):
                    raise PreventUpdate
                return doc
            raise PreventUpdate
        focus = focus_from_trigger(triggered)
        if focus == "":
            raise PreventUpdate
        doc = analysis.with_focus(current, focus)
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("workspace-overview", "children"),
        Input("selection-store", "data"),
        Input("poll", "n_intervals"),
        Input("analysis-tabs", "value"),
        State("project-store", "data"),
    )
    def _render_overview(
        tray: dict | None,
        _tick: int | None,
        tab: str | None,
        project: str | None,
    ):
        if tab != "overview":
            raise PreventUpdate
        return workspace.overview_tab(service, project, tray)

    # -- Analysis tabs inside the workspace ------------------------------

    @app.callback(
        Output("selection-store", "data"),
        Output("analysis-message-store", "data"),
        Output("view-store", "data"),
        Input("url", "pathname"),
        Input("url", "search"),
        Input("project-store", "data"),
        State("selection-store", "data"),
        State("view-store", "data"),
    )
    def _hydrate_workspace_state(
        pathname: str | None,
        search: str | None,
        project: str | None,
        current: dict | None,
        current_view: dict | None,
    ):
        # Shell-only outputs: this fires on every navigation, and Dash
        # raises ReferenceError when a dispatched callback writes a
        # component the current page does not mount (jernerics-8c9).
        # Sole selection-store owner for project changes too: a second
        # writer racing the hydration on the same project-store event
        # could land after it and wipe the hydrated tray.
        tray, tray_error = analysis.hydrate_tray(
            service, project, pathname, search, current
        )
        view, view_error = analysis.hydrate_view(pathname, search, current_view)
        message = tray_error or view_error
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if (
            tray is None
            and message == ""
            and "project-store.data" in triggered
            and (current or {}).get("project") != project
        ):
            # A genuine project switch invalidates the tray — never a
            # same-project replay, a cold start without a project, or a
            # URL edit that only surfaces an error.
            tray = {**analysis.EMPTY_TRAY, "project": project}
        return (
            no_update if tray is None else tray,
            message or "",
            no_update if view is None else view,
        )

    @app.callback(
        Output("url", "search"),
        Input("url", "pathname"),
        Input("selection-store", "data"),
        Input("view-store", "data"),
        State("url", "search"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _sync_selection_url(
        pathname: str | None,
        tray: dict | None,
        view_doc: dict | None,
        current_search: str | None,
        project: str | None,
    ):
        """Sole owner of ``url.search``: mints ``?sel=`` and ``?view=``
        from tray/view edits on the workspace page and drops them when
        navigating away. Only shell-resident ids, so it can fire on any
        page."""
        triggered = {item["prop_id"] for item in dash.callback_context.triggered}
        target = analysis.synced_search(
            service,
            pathname,
            tray,
            current_search,
            project,
            view_doc=view_doc,
            url_navigated="url.pathname" in triggered,
        )
        if target is None:
            raise PreventUpdate
        return target

    @app.callback(
        Output("selection-store", "data", allow_duplicate=True),
        Input("analysis-family-grid", "selectedRows"),
        Input("analysis-expand", "value"),
        State("selection-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_analysis_tray(
        family_rows: list[dict] | None,
        expand_flags: list[str] | None,
        current: dict | None,
    ):
        triggered = dash.callback_context.triggered_prop_ids
        tray = analysis.tray_from_edit(
            None,
            family_rows,
            expand_flags,
            current,
            sweep_edited=False,
            family_edited="analysis-family-grid.selectedRows" in triggered,
            expand_edited="analysis-expand.value" in triggered,
        )
        # AG Grid echoes its programmatic selectedRows back on mount, and
        # session restore replays the stored tray; neither is an edit.
        if tray == (current or {}):
            raise PreventUpdate
        return tray

    @app.callback(
        Output("analysis-expand", "value"),
        Input("selection-store", "data"),
    )
    def _sync_expand_toggle(tray: dict | None):
        return analysis.expand_values(tray)

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-include", "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_include(values: list[str] | None, current: dict | None):
        doc = analysis.view_from_include(current, values)
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("analysis-include", "value"),
        Input("view-store", "data"),
    )
    def _sync_include_controls(doc: dict | None):
        return analysis.include_values(doc)

    @app.callback(
        Output("analysis-error", "children"),
        Input("analysis-message-store", "data"),
    )
    def _show_analysis_message(message: str | None):
        return Error(message) if message else ""

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-tabs", "value"),
        Input("analysis-key", "value"),
        Input("analysis-mode", "value"),
        Input("analysis-reduction", "value"),
        Input("analysis-display", "value"),
        Input("analysis-auto-refresh", "value"),
        Input("analysis-color", "value"),
        Input("analysis-facet", "value"),
        Input("analysis-contour-x", "value"),
        Input("analysis-contour-y", "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_view_state(
        active: str | None,
        keys: list | None,
        mode: str | None,
        reduction: str | None,
        display: str | None,
        auto_flags: list | None,
        color: str | None,
        facet: str | None,
        contour_x: str | None,
        contour_y: str | None,
        current: dict | None,
    ):
        edited = analysis.edited_fields(dash.callback_context.triggered_prop_ids)
        # The control-sync echo fires with every input "changed"; under
        # dash 4's radix components the collected values can arrive
        # positionally scrambled, and applying them would poison the
        # store with a state the codec rejects. Genuine user edits
        # change one or two fields — bursts are echoes, never edits.
        if len(edited) > 2:
            raise PreventUpdate
        doc = analysis.view_from_controls(
            current,
            active=active,
            keys=keys,
            mode=mode,
            reduction=reduction,
            color=color,
            facet=facet,
            contour_x=contour_x,
            contour_y=contour_y,
            trial_display=display,
            auto_refresh="auto" in (auto_flags or []),
            edited=edited,
        )
        # Hydration pushes state to the controls and their echo lands
        # here; an unchanged document is not an edit.
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"context-filter": dash.ALL}, "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_context_filters(_values: list | None, current: dict | None):
        # Re-rendered filter dropdowns fire as additions; the resolved
        # id names the edited dimension and its value comes from the
        # ALL input list (dash 4 child-id resolution).
        dimension, control = pattern_trigger(dash.callback_context)
        if control != "context-filter" or dimension is None:
            raise PreventUpdate
        value = pattern_input_value(
            dash.callback_context.inputs_list, 0, "context-filter", dimension
        )
        doc = analysis.view_from_context_filter(current, dimension, value)
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-series-figure", "clickData"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _focus_from_trace_click(click: dict | None, current: dict | None):
        doc = analysis.view_from_trace_click(current, click)
        if doc is None or doc == (current or {}):
            raise PreventUpdate
        return doc

    app.clientside_callback(
        """
        function(hover, figure, rows) {
            const no = window.dash_clientside.no_update;
            const trial = hover && hover.points && hover.points.length
                ? String(hover.points[0].customdata) : null;
            let figureOut = no;
            if (figure && figure.data) {
                const data = figure.data.map((trace) => {
                    const mine = trace.customdata && trial
                        && String(trace.customdata[0]) === trial;
                    const opacity = trial ? (mine ? 1 : 0.15) : null;
                    const width = trial && mine ? 4 : 2;
                    if (trace.opacity === opacity && trace.line
                        && trace.line.width === width) {
                        return trace;
                    }
                    return Object.assign({}, trace, {
                        opacity,
                        line: Object.assign({}, trace.line, {width}),
                    });
                });
                figureOut = Object.assign({}, figure, {data});
            }
            let rowsOut = no;
            // An empty snapshot must never overwrite the loader's rows:
            // this duplicate output races the server callback, and a
            // stale empty State would empty the grid.
            if (Array.isArray(rows) && rows.length) {
                rowsOut = rows.map((row) => Object.assign({}, row, {
                    _hovered: trial !== null && row.trial_id === trial,
                }));
            }
            return [figureOut, rowsOut];
        }
        """,
        Output("analysis-series-figure", "figure", allow_duplicate=True),
        Output("analysis-family-grid", "rowData", allow_duplicate=True),
        Input("analysis-series-figure", "hoverData"),
        State("analysis-series-figure", "figure"),
        State("analysis-family-grid", "rowData"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("analysis-tabs", "value"),
        Output("analysis-key", "value"),
        Output("analysis-mode", "value"),
        Output("analysis-reduction", "value"),
        Output("analysis-color", "value"),
        Output("analysis-facet", "value"),
        Output("analysis-contour-x", "value"),
        Output("analysis-contour-y", "value"),
        Output("analysis-display", "value"),
        Output("analysis-auto-refresh", "value"),
        Input("view-store", "data"),
        Input("analysis-key", "options"),
        Input("analysis-color", "options"),
        Input("analysis-facet", "options"),
        Input("analysis-contour-x", "options"),
        Input("analysis-contour-y", "options"),
    )
    def _sync_view_controls(
        doc: dict | None,
        key_options: list | None,
        color_options: list | None,
        facet_options: list | None,
        contour_x_options: list | None,
        contour_y_options: list | None,
    ):
        # Dropdown values ride along with their options: a value written
        # before its options exist is dropped by the component and fires
        # back as a spurious clear.
        return analysis.control_values(
            doc,
            {
                "keys": analysis.loaded_option_values(key_options),
                "color": analysis.loaded_option_values(color_options),
                "facet": analysis.loaded_option_values(facet_options),
                "contour_x": analysis.loaded_option_values(contour_x_options),
                "contour_y": analysis.loaded_option_values(contour_y_options),
            },
        )

    @app.callback(
        Output("analysis-catalog", "children"),
        Input("selection-store", "data"),
        Input("analysis-refresh", "n_clicks"),
        Input("poll", "n_intervals"),
        Input("analysis-tabs", "value"),
        State("project-store", "data"),
    )
    def _render_analysis_catalog(
        tray: dict | None,
        _clicks: int | None,
        _tick: int | None,
        tab: str | None,
        project: str | None,
    ):
        # A hidden tab neither queries nor renders; its data loads on
        # activation and on scope/refresh events while active.
        if tab != "catalog":
            raise PreventUpdate
        return analysis.catalog_tab(service, project, tray)

    @app.callback(
        Output("analysis-series-data", "data"),
        Output("analysis-updated", "children"),
        Output("analysis-refresh-store", "data"),
        Input("selection-store", "data"),
        Input("analysis-refresh", "n_clicks"),
        Input("poll", "n_intervals"),
        Input("analysis-tabs", "value"),
        State("project-store", "data"),
        State("view-store", "data"),
        State("analysis-series-data", "data"),
    )
    def _load_series_snapshot(
        tray: dict | None,
        _clicks: int | None,
        _tick: int | None,
        tab: str | None,
        project: str | None,
        view_doc: dict | None,
        snapshot: dict | None,
    ):
        # Only selection, manual refresh, an enabled poll, and tab
        # activation fetch series data. An activation with a usable
        # snapshot renders from the store instead.
        if tab != "series":
            raise PreventUpdate
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if "analysis-tabs.value" in triggered:
            usable, _missing = analysis.snapshot_status(
                snapshot,
                analysis.scope_fingerprint(project, tray),
                (view_doc or analysis.default_view_state())["series"]["reduction"],
                (view_doc or analysis.default_view_state())["series"]["keys"],
            )
            if usable:
                raise PreventUpdate
        now = time.time_ns()
        try:
            return analysis.series_data_outputs(service, project, tray, view_doc, now)
        except Exception as error:
            return analysis.series_data_failure(error, now)

    @app.callback(
        Output("analysis-series-panels", "children"),
        Output("analysis-series-data", "data", allow_duplicate=True),
        Output("analysis-key", "options"),
        Output("analysis-color", "options"),
        Output("analysis-facet", "options"),
        Output("analysis-context-filters", "children"),
        Output("analysis-series-status", "children"),
        Output("analysis-series-figure-store", "data"),
        Input("view-store", "data"),
        Input("analysis-series-data", "data"),
        Input("analysis-tabs", "value"),
        State("project-store", "data"),
        State("selection-store", "data"),
        prevent_initial_call=True,
    )
    def _render_analysis_series(
        view_doc: dict | None,
        snapshot: dict | None,
        tab: str | None,
        project: str | None,
        tray: dict | None,
    ):
        # Presentation rebuilds from the stored snapshot: view-only
        # edits issue zero reads, added keys fetch only the missing
        # ones, and scope/reduction changes rebuild.
        if tab != "series":
            raise PreventUpdate
        if snapshot is None:
            raise PreventUpdate
        try:
            return analysis.series_view_outputs(
                service, project, tray, view_doc, snapshot, time.time_ns()
            )
        except Exception as error:
            return analysis.series_view_failure(error)

    app.clientside_callback(
        """
        function(figure, relayout) {
            if (!figure || !figure.data) {
                return window.dash_clientside.no_update;
            }
            if (!relayout) {
                return figure;
            }
            const ends = {};
            for (const [key, value] of Object.entries(relayout)) {
                const dot = key.indexOf(".");
                if (dot < 0) continue;
                const axis = key.slice(0, dot);
                const rest = key.slice(dot + 1);
                if (!/^[xy]axis[0-9]*$/.test(axis)) continue;
                if (rest === "range" && Array.isArray(value)
                        && value.length === 2) {
                    ends[axis] = [value[0], value[1]];
                } else if (rest === "range[0]" || rest === "range[1]") {
                    const slot = ends[axis] || [null, null];
                    slot[rest === "range[0]" ? 0 : 1] = value;
                    ends[axis] = slot;
                }
            }
            const layout = Object.assign({}, figure.layout);
            let touched = false;
            for (const [axis, pair] of Object.entries(ends)) {
                if (pair[0] === null || pair[1] === null) continue;
                if (!(axis in layout)) continue;
                layout[axis] = Object.assign({}, layout[axis], {
                    range: pair, autorange: false,
                });
                touched = true;
            }
            return touched ? Object.assign({}, figure, {layout}) : figure;
        }
        """,
        Output("analysis-series-figure", "figure"),
        Input("analysis-series-figure-store", "data"),
        State("analysis-series-figure", "relayoutData"),
    )

    @app.callback(
        Output("analysis-message-store", "data", allow_duplicate=True),
        Input("analysis-refresh-store", "data"),
        prevent_initial_call=True,
    )
    def _surface_refresh_error(state: dict | None):
        # Only failures reach the page-level message region; recovery
        # shows in the status line and the next navigation rewrites the
        # message store anyway.
        error = (state or {}).get("error") or ""
        if not error:
            raise PreventUpdate
        return error

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Output({"axis-note": dash.ALL}, "children"),
        Input({"axis-scale": dash.ALL}, "value"),
        Input({"axis-range": dash.ALL}, "value"),
        Input({"axis-min": dash.ALL}, "value"),
        Input({"axis-max": dash.ALL}, "value"),
        Input({"axis-reset": dash.ALL}, "n_clicks"),
        State("view-store", "data"),
        State("analysis-series-data", "data"),
        prevent_initial_call=True,
    )
    def _edit_axis_state(
        _scales: list,
        _ranges: list,
        _lows: list,
        _highs: list,
        _resets: list,
        current: dict | None,
        data: dict | None,
    ):
        # ALL (not MATCH): dash 4 dropdowns mount child elements whose
        # ids extend the pattern id, which breaks single-value MATCH
        # resolution; values are read from inputs_list by resolved id.
        inputs = dash.callback_context.inputs_list
        metric, control = pattern_trigger(dash.callback_context)
        field = control.removeprefix("axis-") if control else ""
        if not metric or field not in {"scale", "range", "min", "max", "reset"}:
            raise PreventUpdate
        doc, note = analysis.axis_state_edit(
            current,
            metric=metric,
            control=field,
            scale=pattern_input_value(inputs, 0, "axis-scale", metric),
            range_mode=pattern_input_value(inputs, 1, "axis-range", metric),
            low=pattern_input_value(inputs, 2, "axis-min", metric),
            high=pattern_input_value(inputs, 3, "axis-max", metric),
            data=data,
        )
        # A refused edit (invalid bounds, log against non-positive data)
        # keeps the last valid axis; the note still explains why. Notes
        # ride the ALL output in picker order, so every panel stays in
        # sync without a re-render.
        notes = analysis.panel_notes(current, data)
        if (current or analysis.default_view_state())["series"]["mode"] == "stacked":
            keys = (current or analysis.default_view_state())["series"]["keys"]
            if metric in keys:
                notes[keys.index(metric)] = note or notes[keys.index(metric)]
        if doc is None and note is None:
            raise PreventUpdate
        return no_update if doc is None else doc, notes

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Output("analysis-overlay-note", "children"),
        Input("analysis-overlay-scale", "value"),
        Input("analysis-overlay-range", "value"),
        Input("analysis-overlay-min", "value"),
        Input("analysis-overlay-max", "value"),
        Input("analysis-overlay-reset", "n_clicks"),
        State("view-store", "data"),
        State("analysis-series-data", "data"),
        prevent_initial_call=True,
    )
    def _edit_overlay_axis(
        scale: str | None,
        range_mode: str | None,
        low: Any,
        high: Any,
        _reset: int | None,
        current: dict | None,
        data: dict | None,
    ):
        control = overlay_axis_control(dash.callback_context.triggered)
        if control is None:
            raise PreventUpdate
        doc, note = analysis.axis_state_edit(
            current,
            metric=None,
            control=control,
            scale=scale,
            range_mode=range_mode,
            low=low,
            high=high,
            data=data,
        )
        if doc is None and note is None:
            raise PreventUpdate
        return no_update if doc is None else doc, note

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"panel-move-up": dash.ALL}, "n_clicks"),
        Input({"panel-move-down": dash.ALL}, "n_clicks"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _move_series_key(_ups: list, _downs: list, current: dict | None):
        metric, control = pattern_trigger(dash.callback_context)
        if metric is None or control not in ("panel-move-up", "panel-move-down"):
            raise PreventUpdate
        doc = analysis.moved_keys(current, metric, control.removeprefix("panel-move-"))
        if doc is None:
            raise PreventUpdate
        return doc

    @app.callback(
        Output("analysis-points", "children"),
        Input("selection-store", "data"),
        Input("analysis-tabs", "value"),
        State("project-store", "data"),
    )
    def _render_analysis_points(
        tray: dict | None, tab: str | None, project: str | None
    ):
        if tab != "points":
            raise PreventUpdate
        return analysis.points_tab(service, project, tray)

    @app.callback(
        Output("analysis-optuna", "children"),
        Output("analysis-contour-x", "options"),
        Output("analysis-contour-y", "options"),
        Input("selection-store", "data"),
        Input("analysis-contour-x", "value"),
        Input("analysis-contour-y", "value"),
        Input("analysis-tabs", "value"),
        State("project-store", "data"),
    )
    def _render_analysis_optuna(
        tray: dict | None,
        x_param: str | None,
        y_param: str | None,
        tab: str | None,
        project: str | None,
    ):
        if tab != "optuna":
            raise PreventUpdate
        return analysis.optuna_tab_content(service, project, tray, x_param, y_param)

    @app.callback(
        Output("analysis-python", "children"),
        Input("selection-store", "data"),
        Input("analysis-tabs", "value"),
        State("project-store", "data"),
    )
    def _render_analysis_python(
        tray: dict | None, tab: str | None, project: str | None
    ):
        if tab != "python":
            raise PreventUpdate
        return analysis.python_tab(service, project, tray)

    # -- Scroll preservation across refreshes ----------------------------

    app.clientside_callback(
        """
        function(clicks, tick) {
            const grids = [];
            document.querySelectorAll(".ag-body-viewport").forEach(
                (viewport) => {
                    let root = viewport;
                    while (root && root !== document.body && !root.id) {
                        root = root.parentElement;
                    }
                    if (!root || !root.id) return;
                    grids.push({
                        id: root.id,
                        top: viewport.scrollTop,
                        left: viewport.scrollLeft,
                    });
                }
            );
            return {
                x: window.scrollX,
                y: window.scrollY,
                grids: grids,
            };
        }
        """,
        Output("scroll-restore-store", "data"),
        Input("analysis-refresh", "n_clicks"),
        Input("poll", "n_intervals"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(state) {
            if (!state || !state.grids) {
                return window.dash_clientside.no_update;
            }
            const restore = () => {
                window.scrollTo(state.x || 0, state.y || 0);
                for (const grid of state.grids) {
                    const root = document.getElementById(grid.id);
                    if (!root) continue;
                    const viewport = root.querySelector(".ag-body-viewport");
                    if (!viewport) continue;
                    viewport.scrollTop = grid.top || 0;
                    viewport.scrollLeft = grid.left || 0;
                }
            };
            window.requestAnimationFrame(() => window.requestAnimationFrame(restore));
            return window.dash_clientside.no_update;
        }
        """,
        Output("scroll-restore-store", "data", allow_duplicate=True),
        Input("analysis-refresh-store", "data"),
        State("scroll-restore-store", "data"),
        prevent_initial_call=True,
    )

    # -- Artifact listing and viewer (jernerics-h5d.14) ------------------

    @app.callback(
        Output("url", "pathname"),
        Input("artifact-grid", "cellClicked"),
        prevent_initial_call=True,
    )
    def _open_artifact(click: dict | None):
        row_id = click.get("rowId") if isinstance(click, dict) else None
        try:
            artifact_id = UUID(str(row_id))
        except ValueError:
            raise PreventUpdate from None
        return artifacts.viewer_href(str(artifact_id))

    @app.callback(
        Output("artifact-rows-grid", "dashGridOptions"),
        Input("artifact-quick-filter", "value"),
        prevent_initial_call=True,
    )
    def _filter_artifact_rows(text: str | None):
        return components.grid_options(quickFilterText=text or "")
