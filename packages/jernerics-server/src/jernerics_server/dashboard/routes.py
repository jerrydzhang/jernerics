"""URL <-> page mapping for the dashboard shell.

Deep links: ``/dashboard`` (project catalog), ``/dashboard/project/<name>``
(workspace sweep grid), ``/dashboard/sweep/<id>``, ``/dashboard/trial/<id>``,
``/dashboard/execution/<id>``, and ``/dashboard/analysis`` (cross-sweep
analysis; its query string carries the selection token). Unknown paths
render the not-found surface. ``polls`` on a PageSpec is only the
route-level default; live pages decide from fetched facts (see
callbacks.page_content).
"""

from dataclasses import dataclass
from typing import Literal

ROUTES_BASE = "/dashboard"

PageKind = Literal[
    "project",
    "workspace",
    "sweep",
    "trial",
    "execution",
    "analysis",
    "not-found",
]

_KINDS: tuple[PageKind, ...] = ("sweep", "trial", "execution")

_PROJECT_PREFIX = f"{ROUTES_BASE}/project/"


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
    if path == f"{ROUTES_BASE}/analysis":
        return PageSpec(kind="analysis")
    if path.startswith(_PROJECT_PREFIX):
        project = path[len(_PROJECT_PREFIX) :].strip("/")
        if project:
            return PageSpec(kind="workspace", object_id=project)
    for kind in _KINDS:
        prefix = f"{ROUTES_BASE}/{kind}/"
        if path.startswith(prefix):
            object_id = path[len(prefix) :].strip("/")
            if object_id:
                return PageSpec(kind=kind, object_id=object_id)
    return PageSpec(kind="not-found")
