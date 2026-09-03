"""Top-level dashboard shell and the project catalog page.

The shell owns all client-side state: ``dcc.Location`` carries the URL,
``project-store`` the active project, ``view-store`` the workspace view
state the ``view=`` parameter round-trips (active tab, series controls,
the selection scope, focus), ``workspace-store`` the per-project browser
controls (quick filter, column filters, sort), ``analysis-message-store``
the URL-hydration message, and ``poll`` is the conditional refresh
interval pages enable or disable through the router callback.
"""

from dash import dcc, html

from . import components
from .analysis import default_view_state
from .routes import ROUTES_BASE
from .service import ProjectSummary

POLL_INTERVAL_MS = 5000


def shell() -> html.Div:
    """Top-level layout: URL state, nav bar, stores, outlet, poller."""
    return html.Div(
        [
            html.Link(
                rel="icon",
                type="image/svg+xml",
                href=f"{ROUTES_BASE}/assets/favicon.svg",
            ),
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
                    html.Button(
                        id="selection-tray",
                        className="tray",
                        style={"display": "none"},
                    ),
                    html.Form(
                        [
                            html.Button("Log out", type="submit", className="logout"),
                        ],
                        action=f"{ROUTES_BASE}/logout",
                        method="post",
                    ),
                ],
            ),
            html.Main(id="page-container", children=[project_page([], 0)]),
            dcc.Store(id="project-store", storage_type="session"),
            dcc.Store(id="analysis-message-store"),
            dcc.Store(id="overview-digest-store"),
            dcc.Store(id="poll-gate-facts-store"),
            dcc.Store(id="view-store", data=default_view_state()),
            dcc.Store(id="workspace-store", storage_type="session"),
            dcc.Store(id="route-store"),
            dcc.Interval(id="poll", interval=POLL_INTERVAL_MS, disabled=True),
        ],
        className="shell",
    )


def project_page(catalog: list[ProjectSummary], now_ns: int) -> html.Div:
    """Project catalog: health counts, recent sweep, last activity."""
    if not catalog:
        return html.Div(
            [
                html.H2("Projects"),
                components.Empty(
                    "No projects yet — tracking data appears here once a "
                    "sweep is ingested."
                ),
            ],
            className="page",
        )
    rows = [
        html.A(
            [
                html.Span(summary.project, className="project-name"),
                html.Span(
                    [
                        components.Badge(f"active {summary.active}", kind="active"),
                        components.Badge(f"stale {summary.stale}", kind="stale"),
                        components.Badge(f"failed {summary.failed}", kind="failed"),
                        *(
                            [
                                components.Badge(
                                    f"archived {summary.archived_sweeps}",
                                    kind="archived",
                                )
                            ]
                            if summary.archived_sweeps
                            else []
                        ),
                        *(
                            [
                                components.Badge(
                                    f"invalid {summary.invalid_sweeps}", kind="invalid"
                                )
                            ]
                            if summary.invalid_sweeps
                            else []
                        ),
                    ],
                    className="project-counts",
                ),
                html.Span(
                    summary.recent_sweep or components.MISSING,
                    className="project-sweep",
                ),
                html.Span(
                    components.relative_time(summary.last_activity_ns, now_ns),
                    className="project-activity",
                ),
            ],
            href=f"{ROUTES_BASE}/project/{summary.project}",
            className="project-row",
        )
        for summary in catalog
    ]
    return html.Div(
        [
            html.H2("Projects"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Span("Project", className="project-name"),
                            html.Span("Execution health", className="project-counts"),
                            html.Span("Recent sweep", className="project-sweep"),
                            html.Span("Last activity", className="project-activity"),
                        ],
                        className="project-row project-head",
                    ),
                    *rows,
                ],
                className="project-list",
            ),
        ],
        className="page",
    )


_KIND_LABELS = {
    "workspace": "Project",
    "artifact": "Artifact",
    "investigation": "Investigation",
}


def missing_object_page(kind: str, object_id: str) -> html.Div:
    """Deep link to an id the store does not know (or a malformed id)."""
    label = _KIND_LABELS.get(kind, kind)
    return html.Div(
        [
            html.H2(f"{label} {object_id}"),
            components.Empty(f"No {label.lower()} matches {object_id} in this store."),
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
