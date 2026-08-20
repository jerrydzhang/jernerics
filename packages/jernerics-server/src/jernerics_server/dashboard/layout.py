"""Top-level dashboard shell and per-page shells.

The shell owns all client-side state: ``dcc.Location`` carries the URL,
``project-store`` the active project, ``selection-store`` the sweep-id
tray (a plain JSON list; the typed ``Selection`` is built per query call
in the service), and ``poll`` is the conditional refresh interval pages
enable or disable through the router callback.
"""

from dash import dcc, html

from . import components
from .routes import ROUTES_BASE, PageSpec

POLL_INTERVAL_MS = 5000

_KIND_LABELS = {
    "sweep": "Sweep",
    "trial": "Trial",
    "execution": "Execution",
}


def shell() -> html.Div:
    """Top-level layout: URL state, nav bar, stores, outlet, poller."""
    return html.Div(
        [
            dcc.Location(id="url", refresh=False),
            html.Header(
                className="nav",
                children=[
                    html.A("jernerics", href=f"{ROUTES_BASE}/", className="brand"),
                    dcc.Dropdown(
                        id="project-picker",
                        placeholder="Project…",
                        clearable=True,
                        className="project-picker",
                    ),
                    html.Span(id="selection-tray", className="tray"),
                    html.Form(
                        [
                            html.Button("Log out", type="submit", className="logout"),
                        ],
                        action=f"{ROUTES_BASE}/logout",
                        method="post",
                    ),
                ],
            ),
            html.Main(id="page-container", children=[project_page([])]),
            dcc.Store(id="project-store", storage_type="session"),
            dcc.Store(
                id="selection-store", storage_type="session", data={"sweeps": []}
            ),
            dcc.Store(id="poll-status"),
            dcc.Interval(id="poll", interval=POLL_INTERVAL_MS, disabled=True),
        ],
        className="shell",
    )


def project_page(projects: list[str]) -> html.Div:
    """Project home: picker summary plus the h5d.12 overview placeholder."""
    if not projects:
        body = components.Empty(
            "No projects yet — tracking data appears here once a sweep is ingested."
        )
    else:
        body = html.Div(
            [
                html.P(
                    f"{len(projects)} project(s) tracked; pick one above.",
                ),
                components.UnderConstruction("Project overview"),
            ]
        )
    return html.Div([html.H2("Projects"), body], className="page")


def object_page(spec: PageSpec) -> html.Div:
    """Sweep/trial/execution focus page: id shown, view under construction."""
    label = _KIND_LABELS[spec.kind]
    return html.Div(
        [
            html.H2(f"{label} {spec.object_id}"),
            components.UnderConstruction(f"{label} detail"),
        ],
        className="page",
    )


def not_found_page(pathname: str) -> html.Div:
    """Unknown dashboard path (client-side 404 surface)."""
    return html.Div(
        [
            html.H2("Not found"),
            components.Error(f"No dashboard route matches {pathname}."),
        ],
        className="page",
    )


def page_layout(spec: PageSpec, pathname: str) -> html.Div:
    """Render the page shell a parsed route denotes."""
    if spec.kind == "project":
        return project_page([])
    if spec.kind == "not-found":
        return not_found_page(pathname)
    return object_page(spec)
