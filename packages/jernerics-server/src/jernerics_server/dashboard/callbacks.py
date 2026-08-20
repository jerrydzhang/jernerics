"""Navigation, project picker, selection tray, and polling callbacks.

Every callback body is a thin wrapper over a pure module-level helper so
the rendering logic stays testable without a browser.
"""

import dash
from dash import Input, Output
from dash.exceptions import PreventUpdate

from . import layout
from .routes import parse_route
from .service import DashboardService


def page_content(
    pathname: str | None, service: DashboardService
) -> tuple[object, bool]:
    """(page shell, poll enabled) for a URL, with live project data."""
    spec = parse_route(pathname)
    if spec.kind == "project":
        return layout.project_page(service.projects()), spec.polls
    page = layout.page_layout(spec, pathname or "")
    return page, spec.polls


def project_options(projects: list[str]) -> list[dict[str, str]]:
    return [{"label": project, "value": project} for project in projects]


def tray_summary(data: dict | None) -> str:
    sweeps = (data or {}).get("sweeps") or []
    return f"{len(sweeps)} sweep(s) in tray"


def register_callbacks(app: dash.Dash, service: DashboardService) -> None:
    @app.callback(
        Output("page-container", "children"),
        Output("poll", "disabled"),
        Input("url", "pathname"),
    )
    def _render_page(pathname: str | None):
        page, polls = page_content(pathname, service)
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
        Output("project-store", "data"),
        Input("project-picker", "value"),
        prevent_initial_call=True,
    )
    def _remember_project(project: str | None):
        return project

    @app.callback(
        Output("selection-store", "data"),
        Input("project-store", "data"),
        prevent_initial_call=True,
    )
    def _clear_selection_on_project_change(project: str | None):
        return {"sweeps": [], "project": project}

    @app.callback(
        Output("selection-tray", "children"),
        Input("selection-store", "data"),
    )
    def _update_tray(data: dict | None):
        return tray_summary(data)

    @app.callback(
        Output("poll-status", "data"),
        Input("poll", "n_intervals"),
        prevent_initial_call=True,
    )
    def _poll_tick(n_intervals: int | None):
        # Scaffold: h5d.12/.13/.14 pages refresh their data here while
        # the poll interval is enabled for their route.
        return {"tick": n_intervals}
