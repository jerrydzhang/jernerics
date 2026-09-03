"""URL <-> page mapping for the dashboard shell.

Deep links: ``/dashboard`` (project catalog), ``/dashboard/project/<name>``
(the persistent workspace; its query string carries the selection token and
the view document), ``/dashboard/project/<name>/investigations`` (the
investigations index), ``/dashboard/project/<name>/investigation/new`` and
``.../investigation/<id>[/edit]`` (the investigation editor; ``<id>``
alone is the investigation workspace, its plain query string carries the
view, member scope, and filters), ``/dashboard/project/<name>/sweep/<id>``
and ``/dashboard/project/<name>/exceptions`` (new-shell pages; routes
exist ahead of their rendering), and ``/dashboard/artifact-view/<hex>``
(the viewer; ``/dashboard/artifact/<hex>`` is the raw download alias
served by the HTTP layer, not a page). Unknown paths render the
not-found surface. ``polls`` on a PageSpec is only the route-level
default; live pages decide from fetched facts (see callbacks.page_content).
"""

from dataclasses import dataclass
from typing import Literal

ROUTES_BASE = "/dashboard"

PageKind = Literal[
    "project",
    "workspace",
    "sweep",
    "exceptions",
    "investigations",
    "investigation",
    "investigation-edit",
    "artifact",
    "not-found",
]

_PROJECT_PREFIX = f"{ROUTES_BASE}/project/"
_ARTIFACT_VIEW_PREFIX = f"{ROUTES_BASE}/artifact-view/"
_INVESTIGATION_SEGMENT = "investigation"
_INVESTIGATIONS_SEGMENT = "investigations"
_SWEEP_SEGMENT = "sweep"
_EXCEPTIONS_SEGMENT = "exceptions"


@dataclass(frozen=True)
class PageSpec:
    """Which page a URL denotes, plus the object it is focused on.
    Project-scoped pages carry the project in ``object_id`` and the
    focused object (investigation or sweep id) in ``sub_id`` (``None``
    for the create flow and the exceptions page)."""

    kind: PageKind
    object_id: str | None = None
    sub_id: str | None = None
    polls: bool = False


NEW_SHELL_KINDS: frozenset[PageKind] = frozenset(
    {
        "project",
        "workspace",
        "sweep",
        "artifact",
        "exceptions",
        "investigations",
        "investigation",
        "investigation-edit",
    }
)
"""Kinds whose pages render the new-shell chrome themselves, so the
legacy nav hides for them. Grows as cutover rounds land their pages;
dies with the demolition task."""


def parse_route(pathname: str | None) -> PageSpec:
    """Map a browser pathname (as reported by dcc.Location) to a page."""
    path = pathname or f"{ROUTES_BASE}/"
    if path in (ROUTES_BASE, f"{ROUTES_BASE}/"):
        return PageSpec(kind="project")
    if path.startswith(_PROJECT_PREFIX):
        segments = [part for part in path[len(_PROJECT_PREFIX) :].split("/") if part]
        spec = _parse_project_segments(segments)
        if spec is not None:
            return spec
    if path.startswith(_ARTIFACT_VIEW_PREFIX):
        artifact_id = path[len(_ARTIFACT_VIEW_PREFIX) :].strip("/")
        if artifact_id:
            return PageSpec(kind="artifact", object_id=artifact_id)
    return PageSpec(kind="not-found")


def _parse_project_segments(segments: list[str]) -> PageSpec | None:
    """The page under ``/project/<name>/...``, or ``None`` when the
    path shape is unknown (the caller renders not-found)."""
    if not segments:
        return None
    project = segments[0]
    if len(segments) == 1:
        return PageSpec(kind="workspace", object_id=project)
    if segments[1] == _INVESTIGATION_SEGMENT:
        if len(segments) == 3 and segments[2] == "new":
            return PageSpec(kind="investigation-edit", object_id=project)
        if len(segments) == 4 and segments[3] == "edit":
            return PageSpec(
                kind="investigation-edit", object_id=project, sub_id=segments[2]
            )
        if len(segments) == 3:
            return PageSpec(kind="investigation", object_id=project, sub_id=segments[2])
        return None
    if segments[1] == _INVESTIGATIONS_SEGMENT:
        if len(segments) == 2:
            return PageSpec(kind="investigations", object_id=project)
        return None
    if len(segments) == 2 and segments[1] == _EXCEPTIONS_SEGMENT:
        return PageSpec(kind="exceptions", object_id=project)
    if len(segments) == 3 and segments[1] == _SWEEP_SEGMENT:
        return PageSpec(kind="sweep", object_id=project, sub_id=segments[2])
    return None
