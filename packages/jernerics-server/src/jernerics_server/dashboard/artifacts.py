"""Artifact listing and viewer renderers (jernerics-h5d.14).

Immutable user artifacts and stored execution logs are first-class
dashboard objects: version rows on the trial and execution pages, and a
viewer page that dispatches on content type / filename / key to a
bounded renderer. Binary bytes never enter callback state — media uses
the authenticated raw URL, text and JSON are read capped from the local
blob store through DashboardService, and everything else falls back to
facts plus a download link. Stored logs get bounded viewing and
download only: no live tail, no search service, no debug controls.
"""

import json
from typing import Any

from dash import dcc, html
from dash_ag_grid import AgGrid

from . import analysis, components
from .components import MISSING, Badge, human_size, short_id
from .routes import ROUTES_BASE
from .service import ArtifactView, DashboardService

JSON_CAP = 1024 * 1024
"""Upper bound for a blob rendered as JSON (tree or summary/rows table)."""

TEXT_CAP = 256 * 1024
"""Upper bound for the first chunk of a text or log view."""

_GRID_DEFAULTS: dict[str, Any] = {
    "sortable": True,
    "filter": True,
    "resizable": True,
    "minWidth": 100,
}


def viewer_href(artifact_id: str) -> str:
    return f"{ROUTES_BASE}/artifact-view/{artifact_id.replace('-', '')}"


def raw_href(artifact_id: str) -> str:
    """The session-authenticated download alias for one artifact."""
    return f"{ROUTES_BASE}/artifact/{artifact_id.replace('-', '')}"


def renderer_name(content_type: str, filename: str, key: str) -> str:
    """Type-level renderer choice (json | log | text | image | audio |
    video | fallback); shape and binary sniffs refine json/text later."""
    ctype = content_type.lower()
    if ctype == "application/json" or filename.lower().endswith(".json"):
        return "json"
    if key in ("stdout", "stderr"):
        return "log"
    if ctype.startswith("text/"):
        return "text"
    for media in ("image", "audio", "video"):
        if ctype.startswith(f"{media}/"):
            return media
    return "fallback"


def _context_text(context: dict[str, Any] | None) -> str:
    if not context:
        return MISSING
    return ", ".join(f"{k}={v}" for k, v in sorted(context.items()))


def _download(view: ArtifactView) -> html.A:
    return html.A("Download", href=raw_href(view.artifact_id), className="download")


def _viewer_header(view: ArtifactView, now_ns: int) -> html.Section:
    facts = components.DataTable(
        ("Fact", "Value"),
        [
            ("Key", view.key),
            ("Version", f"v{view.version} of {view.versions}"),
            ("Filename", view.filename),
            ("Size", f"{human_size(view.size_bytes)} ({view.size_bytes} bytes)"),
            ("Content type", view.content_type),
            ("SHA-256", html.Code(view.sha256 or MISSING)),
            ("Source", view.source),
            ("Context", _context_text(view.context)),
            ("Declared", components.time_cell(view.declared_ns, now_ns)),
            (
                "Received",
                (
                    components.time_cell(view.received_ns, now_ns)
                    if view.received_ns
                    else MISSING
                ),
            ),
            ("State", Badge("available" if view.available else "pending")),
            (
                "Trial",
                html.A(
                    short_id(view.trial_id),
                    href=analysis.workspace_focus_href(
                        view.project, "trial", view.trial_id
                    ),
                ),
            ),
            (
                "Execution",
                (
                    html.A(
                        short_id(view.execution_id),
                        href=analysis.workspace_focus_href(
                            view.project, "execution", view.execution_id
                        ),
                    )
                    if view.execution_id
                    else MISSING
                ),
            ),
            (
                "Sweep",
                html.A(
                    view.sweep_name,
                    href=analysis.workspace_focus_href(
                        view.project, "sweep", view.sweep_id
                    ),
                ),
            ),
        ],
    )
    return html.Section(
        [html.H3("Facts"), facts, _download(view)],
        className="section",
        id="section-artifact-facts",
    )


def _pending_card(view: ArtifactView) -> html.Div:
    return html.Div(
        [
            html.P(
                "blob not received — declared metadata only", className="artifact-note"
            ),
            _download(view),
        ],
        className="artifact-card",
    )


def _fallback_card(view: ArtifactView, note: str | None = None) -> html.Div:
    text = note or f"no inline renderer for {view.content_type}"
    return html.Div(
        [html.P(text, className="artifact-note"), _download(view)],
        className="artifact-card",
    )


def _truncation_note(view: ArtifactView) -> html.P:
    return html.P(
        f"truncated (showing first {human_size(TEXT_CAP)} of "
        f"{human_size(view.size_bytes)}) — download for the full file",
        className="artifact-note",
    )


def _text_body(service: DashboardService, view: ArtifactView, *, log: bool) -> html.Div:
    read = service.read_artifact_text(view.artifact_id, TEXT_CAP)
    if read is None:
        return _pending_card(view)
    text, truncated = read
    if "\x00" in text:
        return _fallback_card(view, note="blob bytes are not text")
    parts: list[Any] = [_truncation_note(view)] if truncated else []
    parts.append(html.Pre(text, className="log-view" if log else "text-view"))
    return html.Div(parts, className="artifact-text")


def _json_node(key: str, value: Any) -> Any:
    if isinstance(value, dict):
        if not value:
            return html.Div(f"{key}: {{}}", className="json-leaf")
        return html.Details(
            [html.Summary(key), *(_json_node(k, v) for k, v in value.items())],
            className="json-branch",
        )
    if isinstance(value, list):
        if not value:
            return html.Div(f"{key}: []", className="json-leaf")
        return html.Details(
            [
                html.Summary(f"{key} · list[{len(value)}]"),
                *(_json_node(f"[{index}]", item) for index, item in enumerate(value)),
            ],
            className="json-branch",
        )
    return html.Div(f"{key}: {json.dumps(value)}", className="json-leaf")


def _json_tree(payload: Any) -> html.Div:
    if isinstance(payload, dict):
        return html.Div(
            [_json_node(key, value) for key, value in payload.items()],
            className="json-tree",
        )
    return html.Div(_json_node("root", payload), className="json-tree")


def _is_summary_rows(payload: Any) -> bool:
    """The inspection.json shape: an object of exactly {summary, rows}."""
    return (
        isinstance(payload, dict)
        and set(payload) == {"summary", "rows"}
        and isinstance(payload["summary"], dict)
        and isinstance(payload["rows"], list)
        and all(isinstance(row, dict) for row in payload["rows"])
    )


def _summary_rows_view(payload: dict[str, Any]) -> html.Div:
    cards = html.Div(
        [
            html.Span(f"{key}: {value}", className="summary-card")
            for key, value in sorted(payload["summary"].items())
        ],
        className="summary-cards",
    )
    rows = payload["rows"]
    columns = [{"headerName": key, "field": key} for key in rows[0]] if rows else []
    grid = AgGrid(
        id="artifact-rows-grid",
        rowData=rows,
        columnDefs=columns,
        defaultColDef=_GRID_DEFAULTS,
        dashGridOptions=components.grid_options(quickFilterText=""),
        className="ag-theme-alpine grid",
    )
    return html.Div(
        [
            cards,
            dcc.Input(
                id="artifact-quick-filter",
                type="text",
                placeholder="Filter rows…",
                className="quick-filter",
            ),
            grid,
        ]
    )


def _json_body(service: DashboardService, view: ArtifactView) -> Any:
    if view.size_bytes > JSON_CAP:
        return _fallback_card(
            view, note=f"JSON over {human_size(JSON_CAP)} is not rendered inline"
        )
    read = service.read_artifact_text(view.artifact_id, JSON_CAP)
    if read is None:
        return _pending_card(view)
    text, _truncated = read
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _text_body(service, view, log=False)
    if _is_summary_rows(payload):
        return _summary_rows_view(payload)
    return _json_tree(payload)


def _content_body(service: DashboardService, view: ArtifactView) -> Any:
    renderer = renderer_name(view.content_type, view.filename, view.key)
    if not view.available:
        return _pending_card(view)
    if renderer == "json":
        return _json_body(service, view)
    if renderer in ("text", "log"):
        return _text_body(service, view, log=renderer == "log")
    if renderer == "image":
        return html.Img(
            src=raw_href(view.artifact_id),
            alt=view.filename,
            className="artifact-image",
        )
    if renderer == "audio":
        return html.Audio(src=raw_href(view.artifact_id), controls=True)
    if renderer == "video":
        return html.Video(
            src=raw_href(view.artifact_id), controls=True, className="artifact-video"
        )
    return _fallback_card(view)


def _breadcrumbs(view: ArtifactView) -> html.Nav:
    """Project > Sweep > Trial > Execution trail back into the focused
    workspace; the artifact itself is the unlinked leaf."""
    hops: list[tuple[str, str]] = [
        (view.project, f"{ROUTES_BASE}/project/{view.project}"),
        (
            view.sweep_name,
            analysis.workspace_focus_href(view.project, "sweep", view.sweep_id),
        ),
        (
            short_id(view.trial_id),
            analysis.workspace_focus_href(view.project, "trial", view.trial_id),
        ),
    ]
    if view.execution_id:
        hops.append(
            (
                short_id(view.execution_id),
                analysis.workspace_focus_href(
                    view.project, "execution", view.execution_id
                ),
            )
        )
    children: list[Any] = []
    for label, href in hops:
        children += [html.A(label, href=href), html.Span("/")]
    children.append(html.Span(view.filename))
    return html.Nav(children, className="crumbs")


def viewer_page(service: DashboardService, view: ArtifactView, now_ns: int) -> html.Div:
    """The artifact viewer: breadcrumbs, factual header, one renderer."""
    return html.Div(
        [
            _breadcrumbs(view),
            html.H2(view.filename),
            html.P(
                f"{view.key} · {short_id(view.artifact_id)}",
                className="artifact-note",
            ),
            _viewer_header(view, now_ns),
            html.Section(
                [html.H3("Content"), _content_body(service, view)],
                className="section",
                id="section-artifact-content",
            ),
        ],
        className="page",
    )
