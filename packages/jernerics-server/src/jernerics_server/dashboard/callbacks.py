"""Navigation, project picker, selection tray, and polling callbacks.

Every callback body is a thin wrapper over a pure module-level helper so
the rendering logic stays testable without a browser. Polling is driven
by the router callback itself: the interval's ``n_intervals`` is an
Input, and each page decides from its fetched facts whether incomplete
work remains — complete pages disable the interval, so terminal pages
never poll.
"""

import time

import dash
from dash import Input, Output, State, no_update
from dash.exceptions import PreventUpdate

from . import analysis, artifacts, layout
from .components import Error
from .routes import parse_route
from .service import DashboardService, TrialDetail

_INCOMPLETE_TRIAL_STATES = ("waiting", "running")


def page_content(
    pathname: str | None,
    service: DashboardService,
    *,
    selected_sweeps: list[str] | None = None,
    now_ns: int | None = None,
) -> tuple[object, bool]:
    """(page, poll enabled) for a URL, with live data.

    ``poll enabled`` is True only while the page's selected work is
    incomplete: waiting/running trials or non-terminal executions.
    """
    spec = parse_route(pathname)
    now = time.time_ns() if now_ns is None else now_ns
    if spec.kind == "project":
        return layout.project_page(service.project_catalog(), now), False
    if spec.kind == "workspace":
        summaries = service.sweep_overview(spec.object_id or "")
        polls = any(summary.incomplete for summary in summaries)
        return (
            layout.workspace_page(
                spec.object_id or "", summaries, selected_sweeps or [], now
            ),
            polls,
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


def register_callbacks(app: dash.Dash, service: DashboardService) -> None:
    @app.callback(
        Output("page-container", "children"),
        Output("poll", "disabled"),
        Input("url", "pathname"),
        Input("poll", "n_intervals"),
        State("selection-store", "data"),
    )
    def _render_page(pathname: str | None, _tick: int | None, selection: dict | None):
        page, polls = page_content(
            pathname,
            service,
            selected_sweeps=(selection or {}).get("sweeps") or [],
        )
        return page, not polls

    @app.callback(
        Output("url", "search", allow_duplicate=True),
        Input("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _clear_search_off_analysis(pathname: str | None, search: str | None):
        """Leaving the analysis page drops its ?sel= token from the URL."""
        if search and parse_route(pathname).kind != "analysis":
            return ""
        raise PreventUpdate

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
    )
    def _show_current_project(project: str | None):
        return project

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
        Input("selection-store", "data"),
    )
    def _update_tray(data: dict | None):
        return analysis.tray_summary(data)

    @app.callback(
        Output("family-lineage-panel", "children"),
        Input("family-grid", "selectedRows"),
        State("family-lineage-store", "data"),
        prevent_initial_call=True,
    )
    def _show_lineage(rows: list[dict] | None, data: dict | None):
        return lineage_panel(rows, data)

    # -- Analysis page (jernerics-h5d.13) ---------------------------------

    @app.callback(
        Output("selection-store", "data"),
        Output("analysis-expand", "value"),
        Output("analysis-error", "children"),
        Input("url", "pathname"),
        Input("url", "search"),
        Input("project-store", "data"),
        State("selection-store", "data"),
    )
    def _hydrate_analysis_tray(
        pathname: str | None,
        search: str | None,
        project: str | None,
        current: dict | None,
    ):
        tray, expand, error = analysis.hydrate_tray(
            service, project, pathname, search, current
        )
        if error is not None:
            return no_update, no_update, Error(error)
        if tray is None:
            raise PreventUpdate
        return tray, expand or [], ""

    @app.callback(
        Output("selection-store", "data", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input("analysis-sweep-grid", "selectedRows"),
        Input("analysis-family-grid", "selectedRows"),
        Input("analysis-expand", "value"),
        State("selection-store", "data"),
        State("url", "pathname"),
        State("project-store", "data"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _edit_analysis_tray(
        sweep_rows: list[dict] | None,
        family_rows: list[dict] | None,
        expand_values: list[str] | None,
        current: dict | None,
        pathname: str | None,
        project: str | None,
        current_search: str | None,
    ):
        tray = analysis.tray_from_picks(sweep_rows, family_rows, expand_values, current)
        # AG Grid echoes its programmatic selectedRows back on mount, and
        # session restore replays the stored tray; neither is an edit,
        # and firing here would rewrite ?sel= over a hydrated token.
        if tray == (current or {}):
            raise PreventUpdate
        if parse_route(pathname).kind != "analysis":
            return tray, no_update
        target = analysis.search_from_tray(service, project, tray, current_search)
        return tray, no_update if target is None else target

    @app.callback(
        Output("analysis-sweep-grid", "rowData"),
        Output("analysis-sweep-grid", "selectedRows"),
        Input("project-store", "data"),
        Input("selection-store", "data"),
    )
    def _load_analysis_sweeps(project: str | None, tray: dict | None):
        if not project:
            return [], []
        return analysis.sweep_picker_rows(service.sweep_overview(project), tray)

    @app.callback(
        Output("analysis-family-grid", "rowData"),
        Output("analysis-family-grid", "selectedRows"),
        Input("selection-store", "data"),
        State("project-store", "data"),
    )
    def _load_analysis_families(tray: dict | None, project: str | None):
        if not project:
            return [], []
        return analysis.family_picker_rows(
            service.analysis_families(project, (tray or {}).get("sweeps") or []),
            tray,
        )

    @app.callback(
        Output("analysis-tray-summary", "children"),
        Input("selection-store", "data"),
    )
    def _summarize_analysis_tray(tray: dict | None):
        return analysis.tray_summary(tray)

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
        artifact_id = (click or {}).get("rowId")
        if not artifact_id:
            raise PreventUpdate
        return artifacts.viewer_href(str(artifact_id))

    @app.callback(
        Output("artifact-rows-grid", "dashGridOptions"),
        Input("artifact-quick-filter", "value"),
        prevent_initial_call=True,
    )
    def _filter_artifact_rows(text: str | None):
        return {"quickFilterText": text or ""}
