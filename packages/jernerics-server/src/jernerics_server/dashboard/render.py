from dataclasses import dataclass, field
from typing import Any

from .components import MISSING

SORT_KINDS = ("numeric", "ns", "string")

_EMPTY_MARKS = (None, "", MISSING)


@dataclass(frozen=True)
class SortColumn:
    """One sortable column: its typed sort ``kind`` plus extra AG Grid
    colDef keys. ``sort_field`` names the raw row field the comparator
    reads when the cell shows derived text (a relative time, a missing
    marker) instead of the sort value."""

    field: str
    header: str
    kind: str
    sort_field: str | None = None
    definition: dict[str, Any] = field(default_factory=dict)


def typed_sort_key(value: Any, kind: str) -> tuple[int, float, str]:
    """Sort key with typed comparison: numbers and ns stamps compare
    numerically, everything else as case-folded text; missing values
    rank after every present value regardless of direction."""
    if value in _EMPTY_MARKS:
        return (1, 0.0, "")
    if kind in ("numeric", "ns"):
        return (0, float(value), "")
    return (0, 0.0, str(value).casefold())


def sort_rows(
    rows: list[dict[str, Any]],
    columns: list[SortColumn],
    sort: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """The row set ordered by the stored AG Grid sort (colId/direction),
    empties last either way — the server-side twin of the grid's own
    comparator, applied over every row so pagination paginates the
    sorted set."""
    by_field = {column.field: column for column in columns}
    ordered = list(rows)
    for entry in reversed(sort or []):
        column = by_field.get(str(entry.get("colId") or ""))
        direction = entry.get("sort")
        if column is None or direction not in ("asc", "desc"):
            continue
        target = column.sort_field or column.field
        present = [row for row in ordered if row.get(target) not in _EMPTY_MARKS]
        empty = [row for row in ordered if row.get(target) in _EMPTY_MARKS]
        present.sort(
            key=lambda row: typed_sort_key(row.get(target), column.kind),
            reverse=direction == "desc",
        )
        ordered = present + empty
    return ordered


def sortable_columns(
    columns: list[SortColumn], sort: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """AG Grid column defs carrying the shared typed comparator and, if
    a stored sort names the column, its initial sort marker."""
    stored = {str(entry.get("colId") or ""): entry.get("sort") for entry in sort or []}
    defs = []
    for column in columns:
        target = column.sort_field or column.field
        definition: dict[str, Any] = {
            "headerName": column.header,
            "field": column.field,
            "comparator": {"function": f"renderTypedSort('{column.kind}', '{target}')"},
            **column.definition,
        }
        if stored.get(column.field) in ("asc", "desc"):
            definition["sort"] = stored[column.field]
        defs.append(definition)
    return defs
