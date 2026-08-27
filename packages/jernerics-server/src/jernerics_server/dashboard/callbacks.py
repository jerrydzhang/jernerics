"""Navigation, project picker, selection tray, and polling callbacks.

Every callback body is a thin wrapper over a pure module-level helper so
the rendering logic stays testable without a browser. Polling is driven
by the router callback itself: the interval's ``n_intervals`` is an
Input, and each page decides from its fetched facts whether incomplete
work remains — complete pages disable the interval, so terminal pages
never poll.
"""

import time
from uuid import UUID

import dash
from dash import Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate

from . import analysis, artifacts, layout
from .components import Error, grid_options
from .routes import parse_route
from .service import (
    WORKSPACE_VIEWS,
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
    TrialDetail,
    view_counts,
    workspace_visible,
)

_INCOMPLETE_TRIAL_STATES = ("waiting", "running")


def page_content(
    pathname: str | None,
    service: DashboardService,
    *,
    selected_sweeps: list[str] | None = None,
    now_ns: int | None = None,
    workspace: dict | None = None,
) -> tuple[html.Div, bool]:
    """(page, poll enabled) for a URL, with live data.

    ``poll enabled`` is True only while the page's selected work is
    incomplete: waiting/running trials or non-terminal executions.
    """
    spec = parse_route(pathname)
    now = time.time_ns() if now_ns is None else now_ns
    if spec.kind == "project":
        return layout.project_page(service.project_catalog(), now), False
    if spec.kind == "workspace":
        project = spec.object_id or ""
        summaries = service.sweep_overview(project)
        state = workspace_state(workspace, project)
        return (
            layout.workspace_page(
                project,
                summaries,
                selected_sweeps or [],
                now,
                visible=workspace_visible(summaries, state["view"]),
                counts=view_counts(summaries),
                state=state,
            ),
            any(summary.incomplete for summary in summaries),
        )
    if spec.kind == "sweep":
        detail = service.sweep_detail(spec.object_id or "")
        if detail is None:
            return (
                layout.missing_object_page("sweep", spec.object_id or ""),
                False,
            )
        return layout.sweep_page(detail, now), detail.overview.incomplete
    if spec.kind == "trial":
        detail = service.trial_detail(spec.object_id or "")
        if detail is None:
            return (
                layout.missing_object_page("trial", spec.object_id or ""),
                False,
            )
        polls = trial_incomplete(detail)
        return layout.trial_page(detail, now), polls
    if spec.kind == "execution":
        detail = service.execution_detail(spec.object_id or "")
        if detail is None:
            return (
                layout.missing_object_page("execution", spec.object_id or ""),
                False,
            )
        return layout.execution_page(detail, now), detail.context["ended_ns"] is None
    if spec.kind == "artifact":
        view = service.artifact_view(spec.object_id or "")
        if view is None:
            return (
                layout.missing_object_page("artifact", spec.object_id or ""),
                False,
            )
        return artifacts.viewer_page(service, view, now), False
    if spec.kind == "analysis":
        # Interactive, user-driven exploration: no polling, so tab state
        # survives until the next navigation.
        return analysis.analysis_page(), False
    return layout.not_found_page(pathname or ""), False


def trial_incomplete(detail: TrialDetail) -> bool:
    """A trial page keeps polling while the family works: the named trial
    (or any generation of it) waits/runs, or an execution is open."""
    if detail.context["state"] in _INCOMPLETE_TRIAL_STATES:
        return True
    return any(record.ended_at is None for record in detail.executions)


def is_initial() -> bool:
    """True inside a callback's initial call (nothing changed to fire
    it) — ``callback_context.triggered`` is falsy exactly then."""
    return not dash.callback_context.triggered


def project_options(projects: list[str]) -> list[dict[str, str]]:
    return [{"label": project, "value": project} for project in projects]


def tray_from_grid(rows: list[dict] | None, current: dict | None) -> dict:
    """Merge AG Grid sweep selection into the unified selection store,
    keeping the active project and analysis-side picks so the tray
    survives navigation."""
    return {
        **analysis.EMPTY_TRAY,
        **(current or {}),
        "sweeps": sorted({str(row["sweep_id"]) for row in rows or []}),
    }


def lineage_panel(rows: list[dict] | None, data: dict | None) -> list[object]:
    """Side-panel lineage for the family selected in the sweep grid."""
    lineage = (data or {}).get("lineage") or []
    root = str(rows[0]["root"]) if rows else None
    return layout.lineage_chain(root, lineage)


def workspace_state(store: dict | None, project: str | None) -> dict:
    """Per-project workspace controls state from the session store."""
    saved = (store or {}).get(project or "") or {}
    view = saved.get("view")
    return {
        "view": view if view in WORKSPACE_VIEWS else "current",
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
    """Session store after one workspace control edit; ``None`` when the
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


def register_callbacks(app: dash.Dash, service: DashboardService) -> None:
    @app.callback(
        Output("page-container", "children"),
        Output("poll", "disabled"),
        Input("url", "pathname"),
        Input("poll", "n_intervals"),
        State("selection-store", "data"),
        State("workspace-store", "data"),
    )
    def _render_page(
        pathname: str | None,
        _tick: int | None,
        selection: dict | None,
        workspace: dict | None,
    ):
        page, polls = page_content(
            pathname,
            service,
            selected_sweeps=(selection or {}).get("sweeps") or [],
            workspace=workspace,
        )
        return page, not polls

    @app.callback(
        Output("project-picker", "options"),
        Input("url", "pathname"),
    )
    def _load_projects(pathname: str | None):
        if pathname is None:
            raise PreventUpdate
        return project_options(service.projects())

    @app.callback(
        Output("project-picker", "value"),
        Input("project-store", "data"),
        Input("url", "search"),
    )
    def _show_current_project(project: str | None, search: str | None):
        # The picker mirrors project-store; with nothing picked, a
        # shared ?sel= token names the project for a fresh session
        # (jernerics-xbx). Picking it here — a plain write through the
        # picker's own callback — runs the exact settle path of a
        # manual pick (picker -> project-store -> hydration re-fires),
        # so the label follows and nothing overrides a chosen project.
        if project:
            return project
        selection, _error = analysis.cold_start(service, search)
        return selection.project if selection else None

    @app.callback(
        Output("project-store", "data"),
        Input("project-picker", "value"),
        prevent_initial_call=True,
    )
    def _remember_project(project: str | None):
        return project

    @app.callback(
        Output("project-store", "data", allow_duplicate=True),
        Input("url", "pathname"),
        prevent_initial_call=True,
    )
    def _adopt_project_from_url(pathname: str | None):
        spec = parse_route(pathname)
        if spec.kind == "workspace":
            return spec.object_id
        raise PreventUpdate

    @app.callback(
        Output("selection-store", "data", allow_duplicate=True),
        Input("project-store", "data"),
        State("selection-store", "data"),
        prevent_initial_call=True,
    )
    def _clear_selection_on_project_change(project: str | None, current: dict | None):
        # Session-store hydration replays the stored project on boot;
        # only a genuine project switch invalidates the tray.
        if (current or {}).get("project") == project:
            raise PreventUpdate
        return {**analysis.EMPTY_TRAY, "project": project}

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
        Output("selection-tray", "href"),
        Input("selection-store", "data"),
        Input("project-store", "data"),
    )
    def _update_tray(tray: dict | None, project: str | None):
        # The tray stays a live summary, but it is also the one-click
        # door into Analysis carrying the current scope.
        return (
            analysis.tray_summary(tray),
            analysis.analysis_href(service, project, tray),
        )

    @app.callback(
        Output("family-lineage-panel", "children"),
        Input("family-grid", "selectedRows"),
        State("family-lineage-store", "data"),
        prevent_initial_call=True,
    )
    def _show_lineage(rows: list[dict] | None, data: dict | None):
        return lineage_panel(rows, data)

    # -- Workspace curation (jernerics-cdf.2) ----------------------------

    @app.callback(
        Output("workspace-store", "data"),
        Input("workspace-view", "value"),
        Input("workspace-quick", "value"),
        Input("sweep-grid", "filterModel"),
        Input("sweep-grid", "columnState"),
        State("project-store", "data"),
        State("workspace-store", "data"),
        prevent_initial_call=True,
    )
    def _remember_workspace(
        view: str | None,
        quick: str | None,
        filters: dict | None,
        columns: list | None,
        project: str | None,
        current: dict | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        fields: dict[str, object] = {}
        if "workspace-view.value" in triggered:
            fields["view"] = view if view in WORKSPACE_VIEWS else "current"
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
        Output("workspace-curation-note", "children"),
        Input("workspace-view", "value"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _switch_workspace_view(view: str | None, project: str | None):
        if view not in WORKSPACE_VIEWS or not project:
            raise PreventUpdate
        visible = workspace_visible(service.sweep_overview(project), view)
        now = time.time_ns()
        return (
            [layout.sweep_grid_row(summary, now) for summary in visible],
            layout.curation_note(visible),
        )

    @app.callback(
        Output("sweep-grid", "dashGridOptions"),
        Input("workspace-quick", "value"),
        State("workspace-store", "data"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _filter_sweep_rows(
        text: str | None,
        _workspace: dict | None,
        _project: str | None,
    ):
        return grid_options(
            rowSelection={"mode": "multiRow"}, quickFilterText=text or ""
        )

    @app.callback(
        Output("ws-analyze", "href"),
        Input("selection-store", "data"),
        State("project-store", "data"),
    )
    def _sync_workspace_analyze(tray: dict | None, project: str | None):
        return analysis.analysis_href(service, project, tray)

    @app.callback(
        Output("ws-archive", "disabled"),
        Output("ws-invalid", "disabled"),
        Output("ws-restore-validity", "disabled"),
        Output("ws-restore", "disabled"),
        Input("sweep-grid", "selectedRows"),
        prevent_initial_call=True,
    )
    def _offer_workspace_transitions(rows: list[dict] | None):
        offered = layout.selection_transitions(rows)
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
        Output("workspace-view", "options"),
        Output("workspace-curation-note", "children", allow_duplicate=True),
        Input("ws-archive", "n_clicks"),
        Input("ws-invalid", "n_clicks"),
        Input("ws-restore-validity", "n_clicks"),
        Input("ws-restore", "n_clicks"),
        State("sweep-grid", "selectedRows"),
        State("ws-reason", "value"),
        State("project-store", "data"),
        State("workspace-store", "data"),
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
        workspace: dict | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        action = triggered_action(triggered, WORKSPACE_ACTIONS)
        if action is None:
            raise PreventUpdate
        sweep_ids = [str(row["sweep_id"]) for row in rows or []]
        if not sweep_ids:
            return (
                layout.action_message(
                    False, "Select sweeps first — actions apply to selected rows."
                ),
                no_update,
                no_update,
                no_update,
                no_update,
            )
        ok, report = apply_curation(service, action, sweep_ids, reason or "")
        summaries = service.sweep_overview(project or "")
        view = workspace_state(workspace, project)["view"]
        visible = workspace_visible(summaries, view)
        now = time.time_ns()
        # Replacing rowData drops the grid's selection silently; writing
        # [] makes the drop an event, so tray and action-bar callbacks
        # re-fire against the post-action state.
        return (
            layout.action_message(ok, report),
            [layout.sweep_grid_row(summary, now) for summary in visible],
            [],
            layout.view_options(view_counts(summaries)),
            layout.curation_note(visible),
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
        State("url", "pathname"),
        State("detail-reason", "value"),
        prevent_initial_call=True,
    )
    def _curate_from_detail(
        _archive: int,
        _invalid: int,
        _validity: int,
        _restore: int,
        pathname: str | None,
        reason: str | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        action = triggered_action(triggered, DETAIL_ACTIONS)
        spec = parse_route(pathname)
        if action is None or spec.kind != "sweep":
            raise PreventUpdate
        sweep_id = spec.object_id or ""
        ok, report = apply_curation(service, action, [sweep_id], reason or "")
        detail = service.sweep_detail(sweep_id)
        banner = (
            layout.detail_curation(detail.overview, time.time_ns())
            if detail is not None
            else no_update
        )
        return layout.action_message(ok, report), banner

    # -- Analysis page (jernerics-h5d.13) ---------------------------------

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
    def _hydrate_analysis_tray(
        pathname: str | None,
        search: str | None,
        project: str | None,
        current: dict | None,
        current_view: dict | None,
    ):
        # Shell-only outputs: this fires on every navigation, and Dash
        # raises ReferenceError when a dispatched callback writes a
        # component the current page does not mount (jernerics-8c9).
        tray, tray_error = analysis.hydrate_tray(
            service, project, pathname, search, current
        )
        view, view_error = analysis.hydrate_view(pathname, search, current_view)
        message = tray_error or view_error
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
        from tray/view edits on the analysis page and drops them when
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
        Input("analysis-sweep-grid", "selectedRows"),
        Input("analysis-family-grid", "selectedRows"),
        Input("analysis-expand", "value"),
        State("selection-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_analysis_tray(
        sweep_rows: list[dict] | None,
        family_rows: list[dict] | None,
        expand_flags: list[str] | None,
        current: dict | None,
    ):
        triggered = dash.callback_context.triggered_prop_ids
        tray = analysis.tray_from_edit(
            sweep_rows,
            family_rows,
            expand_flags,
            current,
            sweep_edited="analysis-sweep-grid.selectedRows" in triggered,
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
        Output("analysis-sweep-grid", "rowData"),
        Output("analysis-sweep-grid", "selectedRows"),
        Input("project-store", "data"),
        Input("selection-store", "data"),
        Input("view-store", "data"),
    )
    def _load_analysis_sweeps(
        project: str | None, tray: dict | None, view_doc: dict | None
    ):
        if not project:
            return [], analysis.mounted_selection([], initial=is_initial())
        rows, selected = analysis.sweep_picker_rows(
            service.sweep_overview(project),
            tray,
            include_archived=bool((view_doc or {}).get("include_archived")),
            include_invalid=bool((view_doc or {}).get("include_invalid")),
        )
        return rows, analysis.mounted_selection(selected, initial=is_initial())

    @app.callback(
        Output("analysis-family-grid", "rowData"),
        Output("analysis-family-grid", "selectedRows"),
        Input("selection-store", "data"),
        State("project-store", "data"),
    )
    def _load_analysis_families(tray: dict | None, project: str | None):
        if not project:
            return [], analysis.mounted_selection([], initial=is_initial())
        rows, selected = analysis.family_picker_rows(
            service.analysis_families(project, (tray or {}).get("sweeps") or []),
            tray,
        )
        return rows, analysis.mounted_selection(selected, initial=is_initial())

    @app.callback(
        Output("analysis-scope-bar", "children"),
        Input("selection-store", "data"),
        Input("project-store", "data"),
    )
    def _render_scope_bar(tray: dict | None, project: str | None):
        return analysis.scope_bar(service, project, tray)

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-tabs", "value"),
        Input("analysis-key", "value"),
        Input("analysis-reduction", "value"),
        Input("analysis-color", "value"),
        Input("analysis-facet", "value"),
        Input("analysis-contour-x", "value"),
        Input("analysis-contour-y", "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_view_state(
        active: str | None,
        key: str | None,
        reduction: str | None,
        color: str | None,
        facet: str | None,
        contour_x: str | None,
        contour_y: str | None,
        current: dict | None,
    ):
        doc = analysis.view_from_controls(
            current,
            active=active,
            key=key,
            reduction=reduction,
            color=color,
            facet=facet,
            contour_x=contour_x,
            contour_y=contour_y,
            edited=analysis.edited_fields(dash.callback_context.triggered_prop_ids),
        )
        # Hydration pushes state to the controls and their echo lands
        # here; an unchanged document is not an edit.
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("analysis-tabs", "value"),
        Output("analysis-key", "value"),
        Output("analysis-reduction", "value"),
        Output("analysis-color", "value"),
        Output("analysis-facet", "value"),
        Output("analysis-contour-x", "value"),
        Output("analysis-contour-y", "value"),
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
                "key": analysis.loaded_option_values(key_options),
                "color": analysis.loaded_option_values(color_options),
                "facet": analysis.loaded_option_values(facet_options),
                "contour_x": analysis.loaded_option_values(contour_x_options),
                "contour_y": analysis.loaded_option_values(contour_y_options),
            },
        )

    @app.callback(
        Output("analysis-catalog", "children"),
        Input("selection-store", "data"),
        State("project-store", "data"),
    )
    def _render_analysis_catalog(tray: dict | None, project: str | None):
        return analysis.catalog_tab(service, project, tray)

    @app.callback(
        Output("analysis-series-figure", "figure"),
        Output("analysis-key", "options"),
        Output("analysis-color", "options"),
        Output("analysis-facet", "options"),
        Input("selection-store", "data"),
        Input("analysis-key", "value"),
        Input("analysis-color", "value"),
        Input("analysis-facet", "value"),
        Input("analysis-reduction", "value"),
        State("project-store", "data"),
    )
    def _render_analysis_series(
        tray: dict | None,
        key: str | None,
        color: str | None,
        facet: str | None,
        reduction: str | None,
        project: str | None,
    ):
        return analysis.series_outputs(
            service, project, tray, key, color, facet, reduction
        )

    @app.callback(
        Output("analysis-points", "children"),
        Input("selection-store", "data"),
        State("project-store", "data"),
    )
    def _render_analysis_points(tray: dict | None, project: str | None):
        return analysis.points_tab(service, project, tray)

    @app.callback(
        Output("analysis-optuna", "children"),
        Output("analysis-contour-x", "options"),
        Output("analysis-contour-y", "options"),
        Input("selection-store", "data"),
        Input("analysis-contour-x", "value"),
        Input("analysis-contour-y", "value"),
        State("project-store", "data"),
    )
    def _render_analysis_optuna(
        tray: dict | None,
        x_param: str | None,
        y_param: str | None,
        project: str | None,
    ):
        return analysis.optuna_tab_content(service, project, tray, x_param, y_param)

    @app.callback(
        Output("analysis-python", "children"),
        Input("selection-store", "data"),
        State("project-store", "data"),
    )
    def _render_analysis_python(tray: dict | None, project: str | None):
        return analysis.python_tab(service, project, tray)

    # -- Artifact listing and viewer (jernerics-h5d.14) ------------------

    @app.callback(
        Output("url", "pathname"),
        Input("artifact-grid", "cellClicked"),
        prevent_initial_call=True,
    )
    def _open_artifact(click: dict | None):
        row_id = (click or {}).get("rowId")
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
        return grid_options(quickFilterText=text or "")
