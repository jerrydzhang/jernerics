from collections.abc import Iterable, Sequence
from typing import Literal
from urllib.parse import quote

from dash import html
from dash.development.base_component import Component

from .components import MISSING
from .routes import ROUTES_BASE

STYLESHEET_HREF = f"{ROUTES_BASE}/assets/page.css"

TABS = ("Overview", "Investigations", "Exceptions")

Limit = Literal["25", "50", "all"]

_TAB_HREFS = {
    "Overview": "{base}/project/{project}",
    "Investigations": "{base}/project/{project}/investigations",
    "Exceptions": "{base}/project/{project}/exceptions",
}


def stylesheet() -> html.Link:
    """The new-shell stylesheet; new pages mount it, legacy pages never see it."""
    return html.Link(rel="stylesheet", href=STYLESHEET_HREF)


def topbar(project: str | None, scope: str = "Active sweeps") -> html.Div:
    """Brand · project scope · log out, one line across the top. A
    catalog page has no project, so it passes ``None`` to drop the
    scopebar; the logout posts to the auth route."""
    children: list[Component | str] = [html.Span("jernerics", className="brand")]
    if project is not None:
        children.append(
            html.Span(
                [
                    project,
                    html.Span("·", className="sep"),
                    "Scope: ",
                    html.B(scope),
                ],
                className="scopebar",
            )
        )
    children.append(html.Span(className="spacer"))
    children.append(
        html.Form(
            html.Button("Log out", type="submit", className="annotate"),
            action=f"{ROUTES_BASE}/logout",
            method="post",
        )
    )
    return html.Div(children, className="topbar")


def tab_href(label: str, project: str) -> str:
    """The URL one tab row entry points at."""
    return _TAB_HREFS[label].format(base=ROUTES_BASE, project=quote(project, safe=""))


def tab_bar(active: str, project: str) -> html.Div:
    """Project tabs with the active one carrying class ``on``."""
    links = [
        html.A(
            label,
            href=tab_href(label, project),
            className="on" if label == active else None,
        )
        for label in TABS
    ]
    return html.Div(links, className="tabs")


def page_container(*children: Component | str, wide: bool = False) -> html.Div:
    """The centered page body (``page-wide`` drops the max width)."""
    return html.Div(list(children), className="page-wide" if wide else "page")


def page_shell(
    active: str,
    project: str,
    *body: Component | str,
    wide: bool = False,
    scope: str = "Active sweeps",
) -> html.Div:
    """One new-shell page: stylesheet, topbar, tab bar, then the body."""
    return html.Div(
        [
            stylesheet(),
            topbar(project, scope),
            page_container(tab_bar(active, project), *body, wide=wide),
        ],
        className="np",
    )


def tile(
    number: Component | str | float,
    label: str,
    *,
    href: str | None = None,
    tone: Literal["crit", "warn"] | None = None,
) -> Component:
    """A count-and-label tile; ``tone`` tints the number."""
    classes = "tile" + (f" {tone}" if tone else "")
    content = [html.Div(number, className="num"), html.Div(label, className="lbl")]
    if href is None:
        return html.Div(content, className=classes)
    return html.A(content, href=href, className=classes)


def tiles(*items: Component) -> html.Div:
    """The tile row."""
    return html.Div(list(items), className="tiles")


def segment(options: Sequence[tuple[str, str | None, bool]]) -> html.Div:
    """A segmented control: (label, href, on); href None renders a span."""
    return html.Div(
        [
            html.A(label, href=href, className="on" if on else None)
            if href is not None
            else html.Span(label, className="on" if on else None)
            for label, href, on in options
        ],
        className="seg",
    )


def limit_segment(active: Limit = "25") -> html.Div:
    """The 25/50/All page-size segment; ``data-limit`` marks each option."""
    return html.Div(
        [
            html.Span(
                value,
                **{"data-limit": value},  # ty: ignore[invalid-argument-type]
                className="on" if value == active else None,
            )
            for value in ("25", "50", "all")
        ],
        className="seg",
    )


def limit_row(*children: Component | str) -> html.Div:
    """A control row (segments, notes, actions) above or below a table."""
    return html.Div(list(children), className="limit-row")


def scroll_table(
    head: Sequence[Component | str],
    rows: Sequence[Component | str],
    *,
    sortable: bool = False,
) -> html.Div:
    """The scroll wrapper with card section and sticky first column."""
    table = html.Table(
        [html.Thead(html.Tr(list(head))), html.Tbody(list(rows))],
        className="sortable" if sortable else None,
    )
    return html.Div(html.Div(table, className="section"), className="table-scroll")


def head_cell(
    label: str,
    *,
    numeric: bool = False,
    sort_dir: Literal["asc", "desc"] | None = None,
) -> html.Th:
    """A column header; ``numeric`` right-aligns, ``sort_dir`` draws the arrow."""
    return html.Th(
        label,
        className="num" if numeric else None,
        **{"data-dir": sort_dir},  # ty: ignore[invalid-argument-type]
    )


def status_dot(status: str, note: str = "") -> html.Span:
    """The status vocabulary: colored dot + text, optional dimmed note."""
    children: list[Component | str] = [status]
    if note:
        children.append(html.Span(note, className="note"))
    return html.Span(children, className=f"st st-{status}")


def artifact_chips(
    artifacts: Iterable[tuple[str, str, str]],
) -> list[Component | str]:
    """Artifact (key, id, filename) triples as mono links joined by ' · '."""
    links = [
        html.A(
            key,
            className="art",
            href=f"{ROUTES_BASE}/artifact-view/{artifact_id.replace('-', '')}",
            title=filename,
            target="_blank",
            rel="noopener",
        )
        for key, artifact_id, filename in artifacts
    ]
    if not links:
        return [MISSING]
    joined: list[Component | str] = []
    for index, link in enumerate(links):
        if index:
            joined.append(" · ")
        joined.append(link)
    return joined


def filter_chip(label: Component | str, remove_href: str = "#") -> html.Span:
    """An active-filter chip with the remove affordance."""
    return html.Span([label, html.A("×", href=remove_href)], className="chip")


def breadcrumbs(crumbs: Sequence[tuple[str, str] | str]) -> html.Div:
    """The crumb trail: (label, href) links, ``›`` separators, plain last."""
    children: list[Component | str] = []
    for index, crumb in enumerate(crumbs):
        if index:
            children.append(html.Span("›", className="dim"))
        if isinstance(crumb, str):
            children.append(crumb)
        else:
            children.append(html.A(crumb[0], href=crumb[1]))
    return html.Div(children, className="crumb")


def inv_nav(
    active: str,
    views: Sequence[tuple[str, str]],
    *,
    python_href: str,
    edit_href: str,
) -> html.Div:
    """The investigation view switcher row with its side actions."""
    seg = [
        html.A(label, href=href, className="on" if label == active else None)
        for label, href in views
    ]
    return html.Div(
        [
            html.Span("Investigation views", className="annotate"),
            html.Div(seg, className="seg", id="inv-tabs"),
            html.Span(className="spacer"),
            html.A("Open in Python", href=python_href, className="btn"),
            html.A("Edit members", href=edit_href, className="btn"),
        ],
        className="limit-row",
    )


def pager(current: int, total: int) -> html.Div:
    """The page buttons (‹, 1..n, ›); empty once a single page remains."""
    if total <= 1:
        return html.Div(className="pager")
    buttons: list[Component | str] = [
        html.Button("‹", disabled=current <= 1),
    ]
    buttons.extend(
        html.Button(str(page), className="on" if page == current else None)
        for page in range(1, total + 1)
    )
    buttons.append(html.Button("›", disabled=current >= total))
    return html.Div(buttons, className="pager")
