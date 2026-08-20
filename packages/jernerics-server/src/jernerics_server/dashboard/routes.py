"""URL <-> page mapping for the dashboard shell.

Deep links: ``/dashboard`` (project home), ``/dashboard/sweep/<id>``,
``/dashboard/trial/<id>``, ``/dashboard/execution/<id>``. Unknown paths
render the not-found surface. ``polls`` marks pages that want the
conditional refresh interval enabled (h5d.13/.14 executions views).
"""

from dataclasses import dataclass
from typing import Literal

ROUTES_BASE = "/dashboard"

PageKind = Literal["project", "sweep", "trial", "execution", "not-found"]

_KINDS: tuple[PageKind, ...] = ("sweep", "trial", "execution")


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
    for kind in _KINDS:
        prefix = f"{ROUTES_BASE}/{kind}/"
        if path.startswith(prefix):
            object_id = path[len(prefix) :].strip("/")
            if object_id:
                return PageSpec(kind=kind, object_id=object_id)
    return PageSpec(kind="not-found")
