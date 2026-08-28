"""URL <-> page mapping for the dashboard shell.

Deep links: ``/dashboard`` (project catalog), ``/dashboard/project/<name>``
(the persistent workspace; its query string carries the selection token and
the view document), and ``/dashboard/artifact-view/<hex>`` (the viewer;
``/dashboard/artifact/<hex>`` is the raw download alias served by the HTTP
layer, not a page). Unknown paths render the not-found surface. ``polls``
on a PageSpec is only the route-level default; live pages decide from
fetched facts (see callbacks.page_content).
"""

from dataclasses import dataclass
from typing import Literal

ROUTES_BASE = "/dashboard"

PageKind = Literal[
    "project",
    "workspace",
    "artifact",
    "not-found",
]

_PROJECT_PREFIX = f"{ROUTES_BASE}/project/"
_ARTIFACT_VIEW_PREFIX = f"{ROUTES_BASE}/artifact-view/"


@dataclass(frozen=True)
class PageSpec:
    """Which page a URL denotes, plus the object it is focused on."""

    kind: PageKind
    object_id: str | None = None
    polls: bool = False


def parse_route(pathname: str | None) -> PageSpec:
    """Map a browser pathname (as reported by dcc.Location) to a page."""
    path = pathname or f"{ROUTES_BASE}/"
    if path in (ROUTES_BASE, f"{ROUTES_BASE}/"):
        return PageSpec(kind="project")
    if path.startswith(_PROJECT_PREFIX):
        project = path[len(_PROJECT_PREFIX) :].strip("/")
        if project:
            return PageSpec(kind="workspace", object_id=project)
    if path.startswith(_ARTIFACT_VIEW_PREFIX):
        artifact_id = path[len(_ARTIFACT_VIEW_PREFIX) :].strip("/")
        if artifact_id:
            return PageSpec(kind="artifact", object_id=artifact_id)
    return PageSpec(kind="not-found")
