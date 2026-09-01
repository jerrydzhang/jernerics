"""Shared presentational pieces: badges, tables, and time formatting."""

import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from dash import dcc, html
from dash.development.base_component import Component

UNKNOWN = "unknown"
MISSING = "—"

TEXT_LIMIT = 120
ELLIPSIS = "…"


def clamp_text(value: Any, limit: int = TEXT_LIMIT) -> str:
    """Single-line bounded text — the shared truncation policy
    (jernerics-l8f): whitespace folds so headers and summary strings
    stay one line tall, long values end with an ellipsis."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + ELLIPSIS


def clamp_tooltip(value: Any, limit: int = TEXT_LIMIT) -> html.Abbr:
    """Clamped text carrying the full value in its title attribute —
    the plain-HTML half of the shared policy (jernerics-l8f)."""
    text = " ".join(str(value).split())
    return html.Abbr(clamp_text(text, limit), title=text)


def clamped_column() -> dict[str, Any]:
    """Column-def fragment applying this policy inside AG Grid
    surfaces: display clamps to ``TEXT_LIMIT`` with an ellipsis, the
    full value rides the title, one click opens it (jernerics-7v6)."""
    return {
        "cellRenderer": "ClampedCell",
        "clampLimit": TEXT_LIMIT,
        "minWidth": 160,
        "maxWidth": 480,
    }


def short_id(identifier: str | None) -> str:
    """Compact identity for grids and tables (first 8 hex chars)."""
    if not identifier:
        return MISSING
    return identifier.replace("-", "")[:8]


def objective_text(objective: float | None) -> str:
    """Grid/table cell text for an objective value; the missing marker
    when the trial never reported one."""
    return MISSING if objective is None else f"{objective:g}"


def relative_time(ns: int | None, now_ns: int | None = None) -> str:
    """ "3m ago"-style recency; ``unknown`` when the fact is missing."""
    if ns is None:
        return UNKNOWN
    now_ns = time.time_ns() if now_ns is None else now_ns
    seconds = max(0, (now_ns - ns) // 1_000_000_000)
    if seconds < 10:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86_400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86_400}d ago"


def absolute_time(ns: int | None) -> str:
    """UTC wall-clock rendering; ``unknown`` when the fact is missing."""
    if ns is None:
        return UNKNOWN
    moment = datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def time_cell(ns: int | None, now_ns: int) -> str:
    """Absolute plus relative recency in one table cell string."""
    if ns is None:
        return UNKNOWN
    return f"{absolute_time(ns)} ({relative_time(ns, now_ns)})"


def time_cell_compact(ns: int | None, now_ns: int) -> html.Td:
    """Single-line timestamp cell: relative recency only, with the
    absolute UTC time as the cell's title tooltip."""
    return html.Td(relative_time(ns, now_ns), title=absolute_time(ns))


def short_host(hostname: str | None) -> str:
    """First DNS label of a host name; a missing host stays missing."""
    if not hostname:
        return MISSING
    return hostname.split(".", 1)[0]


def human_size(size: int | None) -> str:
    """Human byte size ("256 KiB"); exact multiples drop the decimal."""
    if size is None:
        return UNKNOWN
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            digits = 0 if value == int(value) else 1
            return f"{value:.{digits}f} {unit}"
        value /= 1024
    return UNKNOWN


def datetime_to_ns(moment: datetime) -> int:
    """Exact datetime-to-nanoseconds (no float epoch loss)."""
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + (
        delta.microseconds * 1_000
    )


def Badge(label: str, *, kind: str | None = None) -> html.Span:
    """State/monitoring pill; the CSS class comes from ``kind`` (default
    the label's first word)."""
    css_kind = kind or label.split()[0]
    return html.Span(label, className=f"badge badge-{css_kind}")


def DataTable(
    headers: Sequence[str],
    rows: Sequence[Sequence[str | int | float | Component | None]],
) -> html.Table:
    """Plain string-matrix table; cells may be Dash components, and a
    pre-built ``html.Td`` is placed as-is so its own attributes (a
    timestamp tooltip, say) land on the cell itself."""

    def cell(content: str | float | Component | None) -> html.Td:
        return content if isinstance(content, html.Td) else html.Td(content)

    return html.Table(
        [
            html.Thead(html.Tr([html.Th(header) for header in headers])),
            html.Tbody([html.Tr([cell(item) for item in row]) for row in rows]),
        ],
        className="data-table",
    )


def Loading(*children: Component | str) -> dcc.Loading:
    """Wrap content in the standard spinner surface."""
    return dcc.Loading(children=list(children), parent_style={"minHeight": "12rem"})


def Error(message: str) -> html.Div:
    """Something failed while building this view."""
    return html.Div(
        [html.H3("Something went wrong"), html.P(message)],
        className="surface surface-error",
    )


def Empty(message: str) -> html.Div:
    """Nothing to show yet — distinct from failure."""
    return html.Div(
        [html.H3("Nothing here yet"), html.P(message)],
        className="surface surface-empty",
    )


def grid_options(**options: Any) -> dict[str, Any]:
    """``dashGridOptions`` base shared by every AG Grid: cell text stays
    selectable so identifiers (sweep names, trial ids, sha256) can be
    copied, with DOM order preserved so selection spans virtualized
    rows. Pagination stays explicitly off — no grid pages, so the AG
    Grid pagination footer can never appear with a phantom row count.
    Per-grid keys override the base."""
    return {
        "enableCellTextSelection": True,
        "ensureDomOrder": True,
        "pagination": False,
        **options,
    }
