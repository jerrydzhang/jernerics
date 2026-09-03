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
from dash.development.base_component import Component

from . import components, page
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
            dcc.Location(id="navigate", refresh="callback-nav"),
            html.Header(
                id="nav",
                className="nav",
                children=[
                    html.A("jernerics", href=f"{ROUTES_BASE}/", className="brand"),
                    dcc.Dropdown(
                        id="project-picker",
                        placeholder="Project…",
                        clearable=True,
                        className="project-picker",
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
            dcc.Store(id="route-store"),
            dcc.Interval(id="poll", interval=POLL_INTERVAL_MS, disabled=True),
        ],
        className="shell",
    )


def _health(summary: ProjectSummary) -> list[Component | str]:
    """Execution-health cell: one status dot per non-zero state, plus
    curation badges when sweeps were archived or invalidated."""
    parts: list[Component | str] = []
    for status, count in (
        ("running", summary.active),
        ("stale", summary.stale),
        ("failed", summary.failed),
    ):
        if not count:
            continue
        if parts:
            parts.append(" · ")
        parts.append(page.status_dot(status, str(count)))
    if summary.archived_sweeps:
        parts.append(
            components.Badge(f"archived {summary.archived_sweeps}", kind="archived")
        )
    if summary.invalid_sweeps:
        parts.append(
            components.Badge(f"invalid {summary.invalid_sweeps}", kind="invalid")
        )
    if parts:
        return parts
    return [components.MISSING]


def project_page(catalog: list[ProjectSummary], now_ns: int) -> html.Div:
    """Project catalog: health counts, recent sweep, last activity."""
    body: list[Component | str] = [html.H1("Projects")]
    if catalog:
        body.append(
            page.scroll_table(
                [
                    page.head_cell("Project"),
                    page.head_cell("Execution health"),
                    page.head_cell("Recent sweep"),
                    page.head_cell("Last activity"),
                ],
                [
                    html.Tr(
                        [
                            html.Td(
                                html.A(
                                    summary.project,
                                    href=f"{ROUTES_BASE}/project/{summary.project}",
                                )
                            ),
                            html.Td(_health(summary)),
                            html.Td(summary.recent_sweep or components.MISSING),
                            components.time_cell_compact(
                                summary.last_activity_ns, now_ns
                            ),
                        ]
                    )
                    for summary in catalog
                ],
            )
        )
    else:
        body.append(
            components.Empty(
                "No projects yet — tracking data appears here once a sweep is ingested."
            )
        )
    return html.Div(
        [page.stylesheet(), page.topbar(None), page.page_container(*body)],
        className="np",
    )


_KIND_LABELS = {
    "workspace": "Project",
    "sweep": "Sweep",
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
