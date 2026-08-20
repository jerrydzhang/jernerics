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
from dash import Input, Output, State
from dash.exceptions import PreventUpdate

from . import layout
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
    return layout.not_found_page(pathname or ""), False


def trial_incomplete(detail: TrialDetail) -> bool:
    """A trial page keeps polling while the family works: the named trial
    (or any generation of it) waits/runs, or an execution is open."""
    if detail.context["state"] in _INCOMPLETE_TRIAL_STATES:
        return True
    return any(record.ended_at is None for record in detail.executions)


def project_options(projects: list[str]) -> list[dict[str, str]]:
    return [{"label": project, "value": project} for project in projects]


def tray_summary(data: dict | None) -> str:
    sweeps = (data or {}).get("sweeps") or []
    return f"{len(sweeps)} sweep(s) in tray"


def tray_from_grid(rows: list[dict] | None, current: dict | None) -> dict:
    """Merge AG Grid sweep selection into the tray store, keeping the
    active project so the tray survives navigation."""
    sweeps = sorted({str(row["sweep_id"]) for row in rows or []})
    return {"sweeps": sweeps, "project": (current or {}).get("project")}


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
        Output("selection-store", "data"),
        Input("project-store", "data"),
        prevent_initial_call=True,
    )
    def _clear_selection_on_project_change(project: str | None):
        return {"sweeps": [], "project": project}

    @app.callback(
        Output("selection-store", "data", allow_duplicate=True),
        Input("sweep-grid", "selectedRows"),
        State("selection-store", "data"),
        prevent_initial_call=True,
    )
    def _select_sweeps(rows: list[dict] | None, current: dict | None):
        return tray_from_grid(rows, current)

    @app.callback(
        Output("selection-tray", "children"),
        Input("selection-store", "data"),
    )
    def _update_tray(data: dict | None):
        return tray_summary(data)

    @app.callback(
        Output("family-lineage-panel", "children"),
        Input("family-grid", "selectedRows"),
        State("family-lineage-store", "data"),
        prevent_initial_call=True,
    )
    def _show_lineage(rows: list[dict] | None, data: dict | None):
        return lineage_panel(rows, data)
