import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, quote
from uuid import UUID

from dash import dcc, html
from dash.development.base_component import Component
from dash_ag_grid import AgGrid
from jernerics_schema import (
    InvestigationRecord,
    Selection,
    encode_selection,
    materialize_selection,
)

from . import analysis, components, figures, page
from .components import MISSING, short_id
from .render import SortColumn, sort_rows, sortable_columns
from .routes import ROUTES_BASE
from .service import (
    CompareDocument,
    DashboardService,
    InvestigationPreview,
    SweepSummary,
)

INVESTIGATION_VIEWS = analysis.INVESTIGATION_VIEWS
"""Re-exported view vocabulary; the ``view=`` codec owns the names."""


_OVERVIEW_PAGE_SIZE = 25
_OVERVIEW_LIMITS = ("25", "50", "all")
_DEFAULT_OVERVIEW_LIMIT = "25"

_GRID_DEFAULTS: dict[str, Any] = {
    "sortable": True,
    "resizable": True,
    "minWidth": 100,
}

_SWEEP_ROW_ID: Any = "params.data.sweep_id"


def sweep_curation(summary: SweepSummary) -> str:
    """Distinct curation marker for grid cells; invalid outranks archived."""
    if summary.invalid:
        return "invalid"
    if summary.archived:
        return "archived"
    return ""


def hidden_curation(
    summary: SweepSummary, *, include_archived: bool, include_invalid: bool
) -> bool:
    """True when the include controls keep this sweep out of discovery."""
    return (summary.invalid and not include_invalid) or (
        summary.archived and not summary.invalid and not include_archived
    )


def action_message(ok: bool, text: str) -> html.Div:
    """Visible success/failure report for a curation action."""
    return html.Div(text, className=f"action-message {'ok' if ok else 'err'}")


@dataclass(frozen=True)
class OverviewPageUrl:
    """The overview page's URL-carried state; defaults render Active at
    25 rows a page, unfiltered, in the service's activity order."""

    scope_all: bool = False
    overview_filter: str | None = None
    limit: str = _DEFAULT_OVERVIEW_LIMIT
    page: int = 1
    sort: list[dict[str, Any]] | None = None


_OVERVIEW_COLUMNS: list[SortColumn] = [
    SortColumn("name", "Sweep", "string"),
    SortColumn("state", "Status", "string"),
    SortColumn("trials", "Trials", "numeric"),
    SortColumn("best_objective", "Best obj", "numeric"),
    SortColumn("last_activity_ns", "Last activity", "ns"),
]

_OVERVIEW_FILTERS: dict[str, tuple[str, Any]] = {
    "failed": ("with failed executions", lambda s: bool(s.failed)),
    "stale": ("interrupted", lambda s: s.state == "stale"),
    "completed": ("completed", lambda s: s.state == "completed"),
    "no-data": ("no trials yet", lambda s: s.state == "no-data"),
}


def parse_overview_url(search: str | None) -> OverviewPageUrl:
    """The page state carried by the workspace URL's query string;
    unknown values fall back to defaults."""
    params = parse_qs((search or "").lstrip("?"))

    def one(key: str) -> str | None:
        values = params.get(key)
        return values[0] if values else None

    overview_filter = one("f")
    if overview_filter not in _OVERVIEW_FILTERS:
        overview_filter = None
    limit = one("limit")
    if limit not in _OVERVIEW_LIMITS:
        limit = _DEFAULT_OVERVIEW_LIMIT
    sort_col, _, sort_dir = (one("sort") or "").partition(":")
    known = any(column.field == sort_col for column in _OVERVIEW_COLUMNS)
    sort = (
        [{"colId": sort_col, "sort": sort_dir}]
        if known and sort_dir in ("asc", "desc")
        else None
    )
    try:
        current = int(one("page") or "1")
    except ValueError:
        current = 1
    return OverviewPageUrl(
        scope_all=one("scope") == "all",
        overview_filter=overview_filter,
        limit=limit,
        page=max(1, current),
        sort=sort,
    )


def overview_href(project: str, url: OverviewPageUrl) -> str:
    """The workspace URL carrying exactly this page state; defaults stay
    out of the query string."""
    params: list[str] = []
    if url.scope_all:
        params.append("scope=all")
    if url.overview_filter:
        params.append(f"f={url.overview_filter}")
    if url.limit != _DEFAULT_OVERVIEW_LIMIT:
        params.append(f"limit={url.limit}")
    if url.page > 1:
        params.append(f"page={url.page}")
    if url.sort:
        entry = url.sort[0]
        params.append(f"sort={entry['colId']}:{entry['sort']}")
    query = f"?{'&'.join(params)}" if params else ""
    return f"{ROUTES_BASE}/project/{quote(project, safe='')}{query}"


def overview_filter_passes(summary: SweepSummary, overview_filter: str | None) -> bool:
    """Whether one sweep passes the active tile filter; everything
    passes when no tile is active."""
    if not overview_filter:
        return True
    test = _OVERVIEW_FILTERS.get(overview_filter)
    return bool(test) and test[1](summary)


def active_sweeps(summaries: Sequence[SweepSummary]) -> list[SweepSummary]:
    """The Active scope: curated terminal sweeps stay out of discovery
    until the All scope includes them; incomplete sweeps never drop."""
    return [
        summary
        for summary in summaries
        if summary.incomplete
        or not hidden_curation(summary, include_archived=False, include_invalid=False)
    ]


def curation_badges(summary: SweepSummary) -> list[html.Span]:
    """State badges for a curated sweep; a sweep can carry both."""
    badges: list[html.Span] = []
    if summary.invalid:
        badges.append(html.Span("invalid", className="badge invalid"))
    if summary.archived:
        badges.append(html.Span("archived", className="badge archived"))
    return badges


def elided_prefix(names: Sequence[str]) -> str:
    """The shared leading prefix of every name, trimmed back to a
    meaningful boundary — the dimmed link prefix."""
    if len(names) < 2:
        return ""
    prefix = names[0]
    for name in names[1:]:
        while prefix and not name.startswith(prefix):
            prefix = prefix[:-1]
    return prefix.rstrip("_-0123456789")


def sweep_link(project: str, summary: SweepSummary, prefix: str) -> html.A:
    """The sweep-name link: dimmed shared prefix, bold remainder."""
    inner = (
        summary.name[len(prefix) :]
        if prefix and summary.name.startswith(prefix)
        else summary.name
    )
    return html.A(
        [
            *([html.Span(prefix, className="pfx")] if prefix else []),
            html.Span(inner, className="sfx"),
        ],
        href=(
            f"{ROUTES_BASE}/project/{quote(project, safe='')}/sweep/{summary.sweep_id}"
        ),
        className="sweep-link",
    )


def failure_signal(summary: SweepSummary) -> html.Span | None:
    """The name-cell diagnosis: systematic vs isolated failed executions,
    then executions lost without a terminal event."""
    parts: list[Component | str] = []
    trials = summary.trials
    if summary.failed:
        noun = "trial" if trials == 1 else "trials"
        if trials and summary.failed >= trials:
            head = "the only" if trials == 1 else "all"
            parts.append(
                html.Span(
                    f"{head} {trials} {noun} failed — systematic",
                    className="crit-text",
                )
            )
        else:
            parts.append(
                f"{summary.failed} failed execution"
                f"{'s' if summary.failed != 1 else ''} across {trials} {noun}"
                "— isolated"
            )
    if summary.stale:
        parts.append(
            html.Span(
                f"{summary.stale} lost — no terminal event", className="warn-text"
            )
        )
    if not parts:
        return None
    joined: list[Component | str] = []
    for index, part in enumerate(parts):
        if index:
            joined.append(" \u00b7 ")
        joined.append(part)
    return html.Span(joined, className="diag")


def best_objective_text(summary: SweepSummary) -> str:
    """The Best obj cell; four significant digits like the prototype."""
    if summary.best_objective is None:
        return MISSING
    return f"{summary.best_objective:.4g}"


def overview_row(
    project: str, summary: SweepSummary, prefix: str, now_ns: int
) -> html.Tr:
    """One table row: selection checkbox, name link with badges and
    diagnosis, status, trials, best objective, last activity."""
    diagnosis = failure_signal(summary)
    return html.Tr(
        [
            html.Td(
                dcc.Checklist(
                    options=[{"label": "", "value": summary.sweep_id}],
                    value=[],
                    id={"sel-sweep": summary.sweep_id},
                    inputClassName="sel-sweep",
                ),
                className="selbox",
            ),
            html.Td(
                [
                    sweep_link(project, summary, prefix),
                    *curation_badges(summary),
                    *([diagnosis] if diagnosis else []),
                ]
            ),
            html.Td(page.status_dot(summary.state)),
            html.Td(
                f"{summary.trials_complete}/{summary.trials}"
                if summary.trials
                else MISSING,
                className="num",
            ),
            html.Td(best_objective_text(summary), className="num"),
            html.Td(
                components.relative_time(summary.latest_submitted_ns, now_ns),
                className="num",
            ),
        ],
        className="sweep-row",
    )


def selection_bar() -> html.Div:
    """The row-selection bar; the checkbox callback shows it, counts the
    picked sweeps, and aims Create Investigation at the editor seed."""
    return html.Div(
        [
            html.Span("", id="sel-count", className="num"),
            html.A(
                "Create Investigation",
                id="sel-create",
                className="btn-primary",
                href="#",
            ),
            html.Button("Clear", id="sel-clear"),
        ],
        id="selbar",
        className="bulkbar",
        hidden=True,
    )


def _tile_href(project: str, filter_key: str) -> str:
    return overview_href(project, OverviewPageUrl(overview_filter=filter_key))


def overview_tiles(scoped: Sequence[SweepSummary], project: str) -> list[Any]:
    """The prototype's four working tiles; every tile is a link that
    filters the table and carries its own way back."""
    failing = [summary for summary in scoped if summary.failed]
    stale = sum(1 for summary in scoped if summary.state == "stale")
    completed = sum(1 for summary in scoped if summary.state == "completed")
    no_data = sum(1 for summary in scoped if summary.state == "no-data")
    return [
        page.tile(
            sum(summary.failed for summary in failing),
            f"failed executions \u00b7 {counted_sweeps(len(failing))}",
            tone="crit" if failing else None,
            href=_tile_href(project, "failed"),
        ),
        page.tile(
            stale,
            "interrupted runs",
            tone="warn" if stale else None,
            href=_tile_href(project, "stale"),
        ),
        page.tile(completed, "completed sweeps", href=_tile_href(project, "completed")),
        page.tile(
            no_data,
            "sweeps with no trials yet",
            href=_tile_href(project, "no-data"),
        ),
    ]


def overview_page(
    service: DashboardService,
    project: str,
    *,
    url: OverviewPageUrl | None = None,
    now_ns: int | None = None,
) -> html.Div:
    """The project Overview per the approved prototype: heading, scope
    line, operational tiles, and one Sweeps section — a single paginated
    sortable table whose checkboxes feed Create Investigation."""
    state = url or OverviewPageUrl()
    now = time.time_ns() if now_ns is None else now_ns
    summaries = service.sweep_overview(project)
    active = active_sweeps(summaries)
    visible = summaries if state.scope_all else active
    curated_n = len(summaries) - len(active)
    if state.scope_all:
        scope_line = f"All sweeps — including {curated_n} archived/invalid"
    elif curated_n:
        scope_line = f"Active sweeps — hides {curated_n} archived/invalid"
    else:
        scope_line = "Active sweeps"
    activity = max(
        (
            summary.latest_submitted_ns
            for summary in visible
            if summary.latest_submitted_ns is not None
        ),
        default=None,
    )
    sub = html.P(
        f"{scope_line} \u00b7 last activity "
        + ("never" if activity is None else components.relative_time(activity, now)),
        className="sub",
    )

    def shell(*body: Any) -> html.Div:
        return page.page_shell(
            "Overview", project, html.H1("Overview"), *body, scope=scope_line
        )

    if not summaries:
        return shell(components.Empty(f"No sweeps tracked for project {project} yet."))
    if not visible:
        return shell(
            page.tiles(*overview_tiles(visible, project)),
            components.Empty(
                f"No current sweeps in project {project}; archived or invalid "
                "sweeps stay hidden until the scope includes them."
            ),
        )
    filtered = [
        summary
        for summary in visible
        if overview_filter_passes(summary, state.overview_filter)
    ]
    ordered = sort_rows(
        [
            {
                "summary": summary,
                "name": summary.name,
                "state": summary.state,
                "trials": summary.trials,
                "best_objective": summary.best_objective,
                "last_activity_ns": summary.latest_submitted_ns,
            }
            for summary in filtered
        ],
        _OVERVIEW_COLUMNS,
        state.sort,
    )
    size = len(filtered) if state.limit == "all" else int(state.limit)
    total_pages = max(1, -(-len(filtered) // size)) if size else 1
    current = min(state.page, total_pages)
    start = 0 if state.limit == "all" else (current - 1) * size
    page_rows = ordered[start : start + size]
    shown_from = start + 1 if filtered else 0
    shown_to = start + len(page_rows)
    note = f"showing {shown_from}\u2013{shown_to} of {len(filtered)}"
    if state.overview_filter:
        note += f" (filtered from {len(visible)})"
    active_sort = state.sort[0] if state.sort else None

    def sorted_href(field: str) -> str:
        direction = (
            "desc"
            if active_sort
            and active_sort["colId"] == field
            and active_sort["sort"] == "asc"
            else "asc"
        )
        return overview_href(
            project,
            replace(state, sort=[{"colId": field, "sort": direction}], page=1),
        )

    columns = [
        html.Th(className="selbox"),
        *(
            page.head_cell(
                column.header,
                numeric=column.kind != "string",
                sort_dir=(
                    active_sort["sort"]
                    if active_sort and active_sort["colId"] == column.field
                    else None
                ),
                href=sorted_href(column.field),
            )
            for column in _OVERVIEW_COLUMNS
        ),
    ]
    scope_seg = page.segment(
        [
            (
                f"Active ({len(active)})",
                overview_href(
                    project, OverviewPageUrl(overview_filter=state.overview_filter)
                ),
                not state.scope_all,
            ),
            (
                f"All ({len(summaries)})",
                overview_href(
                    project,
                    OverviewPageUrl(
                        scope_all=True, overview_filter=state.overview_filter
                    ),
                ),
                state.scope_all,
            ),
        ]
    )
    limit_seg = page.segment(
        [
            (
                value,
                overview_href(project, replace(state, limit=value, page=1)),
                value == state.limit,
            )
            for value in _OVERVIEW_LIMITS
        ]
    )
    prefix = elided_prefix([summary.name for summary in filtered])
    body = [
        sub,
        page.tiles(*overview_tiles(visible, project)),
        html.H2("Sweeps"),
        selection_bar(),
        page.limit_row(scope_seg, limit_seg, html.Span(note, className="annotate")),
        *(
            [
                page.limit_row(
                    page.filter_chip(
                        f"{counted_sweeps(len(filtered))} "
                        f"{_OVERVIEW_FILTERS[state.overview_filter][0]}",
                        remove_href=overview_href(
                            project, replace(state, overview_filter=None, page=1)
                        ),
                    )
                )
            ]
            if state.overview_filter
            else []
        ),
        page.pager(
            current,
            total_pages,
            href=lambda target: overview_href(project, replace(state, page=target)),
        ),
        page.scroll_table(
            columns,
            [overview_row(project, row["summary"], prefix, now) for row in page_rows],
            sortable=True,
        ),
    ]
    return shell(*body)


def overview_polls(
    service: DashboardService, project: str, url: OverviewPageUrl
) -> bool:
    """Live while any sweep in the visible scope still has work."""
    summaries = service.sweep_overview(project)
    visible = summaries if url.scope_all else active_sweeps(summaries)
    return any(summary.incomplete for summary in visible)


def counted_sweeps(count: int) -> str:
    """``count`` with the noun form that matches it."""
    return f"{count} sweep" if count == 1 else f"{count} sweeps"


_INVESTIGATION_VIEW_TITLES = {
    "compare": "Compare",
    "series": "Series",
    "points": "Points",
    "search": "Search",
}


def investigations_index_href(project: str) -> str:
    """The workspace URL showing the project's Investigations tab."""
    doc = dict(analysis.default_view_state(), active="investigations")
    return f"{ROUTES_BASE}/project/{project}?view={analysis.encode_view_state(doc)}"


def investigation_crumb(
    project: str,
    name: str,
    view_label: str | None = None,
    member_label: str | None = None,
) -> html.Div:
    """``project › Investigations › name › view › member``; the trailing
    view and member labels are omitted on pages that are their own
    destination. The member label is a live region: the member-scope
    callback rewrites it without a page remount."""
    parts: list[Any] = [
        html.A(project, href=f"{ROUTES_BASE}/project/{project}"),
        html.Span(className="dim"),
        html.A("Investigations", href=investigations_index_href(project)),
        html.Span(className="dim"),
        html.Span(name),
    ]
    if view_label:
        parts.append(html.Span(className="dim"))
        parts.append(html.Span(view_label))
    parts.append(
        html.Span(
            [html.Span(className="dim"), html.Span(member_label)]
            if member_label
            else [],
            id={"inv-crumb-member": "scope"},
        )
    )
    return html.Div(parts, className="crumb")


def _views_row(active: str) -> list[Any]:
    """The view switcher; a clientside callback flips the ``on`` mark,
    the view callback carries the state into the URL."""
    return [
        html.Span("Investigation views", className="annotate"),
        html.Div(
            [
                html.Button(
                    _INVESTIGATION_VIEW_TITLES[view],
                    id={"inv-view": view},
                    className="on" if view == active else "",
                )
                for view in INVESTIGATION_VIEWS
            ],
            className="seg",
        ),
    ]


def member_scope_row(member_label: str | None) -> html.Div:
    """The member-scope line: the visible scope fact plus the one-click
    way back to the full cohort (hidden while unscoped)."""
    return html.Div(
        [
            html.Span(
                f"Scoped to member {member_label}" if member_label else "",
                id={"inv-member-note": "scope"},
                className="annotate",
            ),
            html.Button(
                "All members",
                id={"inv-member-clear": "scope"},
                n_clicks=0,
                style={} if member_label else {"display": "none"},
            ),
        ],
        className="member-row",
    )


def python_panel(record: InvestigationRecord, member: str | None = None) -> list:
    """The effective membership (the one member when narrowed) as an
    encoded Selection token plus the runnable handoff snippet."""
    selection = materialize_selection(record)
    if member:
        selection = Selection(project=record.project, sweeps=(UUID(member),))
    token = encode_selection(selection)
    snippet = analysis.python_snippet(token, record.project, "http://localhost:8000")
    pre_style = {"whiteSpace": "pre", "overflowX": "auto"}
    return [
        html.P(
            "The token decodes to the exact effective membership via "
            "jernerics_schema.decode_selection; point TrackingClient at "
            "your server.",
            className="hint",
        ),
        html.Div(
            [
                html.Pre(token, className="config-json", style=pre_style),
                dcc.Clipboard(content=token),
            ],
            className="snippet-row",
        ),
        html.Div(
            [
                html.Pre(snippet, className="config-json", style=pre_style),
                dcc.Clipboard(content=snippet),
            ],
            className="snippet-row",
        ),
    ]


def open_in_python(
    record: InvestigationRecord, member: str | None = None
) -> html.Details:
    """The Open in Python disclosure; the token content is a live region
    the member-scope callback re-renders."""
    return html.Details(
        [
            html.Summary("Open in Python", className="btn-primary"),
            html.Div(
                python_panel(record, member),
                id={"inv-python": "content"},
            ),
        ],
        className="py-details",
    )


def coverage_strip(doc: CompareDocument) -> html.Div:
    """Members / valid / invalid / with-outcome / incomplete — every
    number read straight off the derived member rows."""
    members = doc.members
    invalid = sum(1 for member in members if member.invalid)
    cells = [
        ("Members", len(members)),
        ("Valid", len(members) - invalid),
        ("Marked invalid (excluded by default)", invalid),
        ("With outcome", sum(1 for member in members if member.usable > 0)),
        (
            "Incomplete",
            sum(1 for member in members if member.state != "completed"),
        ),
    ]
    return html.Div(
        [
            html.Div(
                [html.Span(label, className="lbl"), html.B(value, className="num")],
                className="stat",
            )
            for label, value in cells
        ],
        className="coverage-strip",
    )


_COMPARE_MEMBER_COLUMNS: list[SortColumn] = [
    SortColumn(
        "factor",
        "Factor",
        "string",
        definition={"valueFormatter": {"function": "renderMissing(x)"}},
    ),
    SortColumn("name", "Sweep", "string"),
    SortColumn("state", "State", "string"),
    SortColumn(
        "expected_trials",
        "Expected trials",
        "numeric",
        definition={"valueFormatter": {"function": "renderMissing(x)"}},
    ),
    SortColumn("completed", "Completed", "numeric"),
    SortColumn("usable", "Usable (with outcome)", "numeric"),
    SortColumn("curation", "Curation", "string"),
]


def _compare_member_rows(doc: CompareDocument, project: str) -> list[dict[str, Any]]:
    return [
        {
            "sweep_id": member.sweep_id,
            "factor": member.factor_value,
            "name": member.name,
            "link_href": analysis.workspace_focus_href(
                project, "sweep", member.sweep_id
            ),
            "link_label": member.name,
            "state": member.state,
            "expected_trials": member.expected_trials,
            "completed": member.completed,
            "usable": member.usable,
            "curation": (
                " ".join(
                    name
                    for flag, name in (
                        (member.invalid, "invalid"),
                        (member.archived, "archived"),
                    )
                    if flag
                )
                or MISSING
            ),
        }
        for member in doc.members
    ]


def _matched_grid(
    doc: CompareDocument, labels: dict[str, str], outcome: str
) -> html.Section:
    """Signatures matched by two or more analyzable members; one
    numeric column per member, missing stays missing."""
    shared = [row for row in doc.signatures if row.matched >= 2]
    columns = [
        SortColumn("signature", "Signature", "string"),
        *(
            SortColumn(
                sweep_id,
                labels.get(sweep_id, short_id(sweep_id)),
                "numeric",
                definition={"valueFormatter": {"function": "renderMissing(x)"}},
            )
            for sweep_id in doc.analyzable
        ),
    ]
    rows = [
        {
            "signature": row.label,
            **{sweep_id: row.values.get(sweep_id) for sweep_id in doc.analyzable},
        }
        for row in shared
    ]
    common = sum(1 for row in shared if row.common)
    return html.Section(
        [
            html.H3(f"Matched comparison ({outcome})"),
            html.P(
                f"{len(shared)} signatures matched by ≥2 analyzable members · "
                f"{common} common to all {len(doc.analyzable)} · medians pool "
                "matched trials; no imputation, no outliers suppressed.",
                className="overview-sub",
            ),
            AgGrid(
                id="compare-matched-grid",
                rowData=sort_rows(rows, columns, None),
                columnDefs=sortable_columns(columns),
                defaultColDef=_GRID_DEFAULTS,
                dashGridOptions=components.grid_options(),
                getRowId="params.data.signature",
                className="ag-theme-alpine grid",
            ),
        ],
        className="section compare-matched",
    )


def _members_grid(doc: CompareDocument, project: str) -> html.Section:
    return html.Section(
        [
            html.H3("Members"),
            AgGrid(
                id="compare-members-grid",
                rowData=sort_rows(
                    _compare_member_rows(doc, project),
                    _COMPARE_MEMBER_COLUMNS,
                    None,
                ),
                columnDefs=sortable_columns(_COMPARE_MEMBER_COLUMNS),
                defaultColDef=_GRID_DEFAULTS,
                dashGridOptions=components.grid_options(),
                getRowId=_SWEEP_ROW_ID,
                className="ag-theme-alpine grid",
            ),
        ],
        className="section compare-members",
    )


def compare_empty_state(doc: CompareDocument, include_invalid: bool) -> html.Div:
    """The honest empty state: an analysis set with nothing to compare
    names exactly who is excluded and why."""
    members_total = len(doc.members)
    no_outcome = members_total - len(doc.analyzable)
    if doc.analyzable:
        return html.Div(
            html.P(
                f"{no_outcome} of {members_total} members have no outcome "
                "data; the rest are compared below."
            ),
            className="empty-state",
        )
    if include_invalid:
        return html.Div(
            html.P(
                f"None of the {members_total} members has outcome data — "
                "nothing to compare."
            ),
            className="empty-state",
        )
    return html.Div(
        html.P(
            "No analyzable members in the analysis set — "
            f"{doc.excluded_data_bearing} data-bearing members are marked "
            "invalid (excluded by default) and "
            f"{no_outcome - doc.excluded_data_bearing} have no outcome data. "
            "Tick “include invalid members in analysis” to see the real "
            "comparison."
        ),
        className="empty-state",
    )


def compare_children(
    doc: CompareDocument,
    project: str,
    outcome: str,
    include_invalid: bool,
) -> list[Any]:
    """The Compare view's body for one analysis set: heatmap + ranking
    over the common signatures, the matched-signature table, and the
    member inventory. No analyzable members or no global overlap each
    render their honest state instead of a manufactured ranking."""
    members = {member.sweep_id: member for member in doc.members}
    labels = {
        sweep_id: member.factor_value or member.name
        for sweep_id, member in members.items()
    }
    common = [row for row in doc.signatures if row.common]
    body: list[Any] = []
    if not doc.analyzable:
        body.append(compare_empty_state(doc, include_invalid))
    elif not common:
        body.append(
            html.Div(
                html.P(
                    f"No sampled signature is completed by all "
                    f"{len(doc.analyzable)} analyzable members — no global "
                    "overlap. Pairwise matches are listed below; no ranking "
                    "is manufactured."
                ),
                className="empty-state",
            )
        )
    else:
        column_labels = [row.label for row in common]
        row_labels = [labels.get(sweep_id, sweep_id) for sweep_id in doc.analyzable]
        values = [
            [row.values.get(sweep_id) for row in common] for sweep_id in doc.analyzable
        ]
        medians = []
        matched_counts = []
        for sweep_id in doc.analyzable:
            pooled = [
                row.values.get(sweep_id)
                for row in common
                if row.values.get(sweep_id) is not None
            ]
            medians.append(sum(pooled) / len(pooled) if pooled else 0.0)
            matched_counts.append(len(pooled))
        keys = ", ".join(doc.signature_keys) if doc.signature_keys else "none observed"
        body.extend(
            [
                html.Section(
                    [
                        html.H3("Outcome heatmap"),
                        html.P(
                            "factor by exact sampled signature "
                            f"({keys}) — no imputation, no outliers "
                            "suppressed.",
                            className="overview-sub",
                        ),
                        dcc.Graph(
                            figure=figures.compare_heatmap(
                                row_labels, column_labels, values
                            ),
                            config={"displayModeBar": False},
                        ),
                    ],
                    className="section compare-heatmap",
                ),
                html.Section(
                    [
                        html.H3("Median over common signatures"),
                        dcc.Graph(
                            figure=figures.compare_ranking(
                                row_labels, medians, matched_counts
                            ),
                            config={"displayModeBar": False},
                        ),
                    ],
                    className="section compare-ranking",
                ),
            ]
        )
    if doc.signatures:
        body.append(_matched_grid(doc, labels, outcome))
    body.append(_members_grid(doc, project))
    return body


def investigation_page(
    service: DashboardService,
    project: str,
    investigation_id: str,
    search: str | None = None,
) -> html.Div:
    """The Investigation workspace: breadcrumb, header naming the
    investigation, the view row, the member-scope line, the Open in
    Python / Edit members actions, and every view's region — Compare
    content, the Series tray, the Points set, and the member Search.
    The ``?view=`` document carries the active view and the member
    scope; an unknown member falls back to all members."""
    if not project or not investigation_id:
        return html.Div(components.Empty("No investigation requested."))
    detail = service.investigation_detail(investigation_id)
    record = detail.investigation
    decoded, error = analysis.decoded_view_param(search)
    doc = decoded or analysis.default_view_state()
    tray, scoped = analysis.investigation_scope_state(
        record.members, doc["inv"]["member"]
    )
    active = doc["inv"]["view"]
    member_label = None
    if scoped:
        focused = service.sweep_detail(scoped)
        member_label = focused.overview.name if focused else short_id(scoped)
    compare = service.investigation_compare(investigation_id)
    invalid = sum(1 for member in compare.members if member.invalid)
    display = {"display": "block"}
    hidden = {"display": "none"}
    regions = [
        html.Div(
            [
                coverage_strip(compare),
                *(
                    [
                        dcc.Checklist(
                            id={"inv-compare-toggle": "include"},
                            options=[
                                {
                                    "label": " include invalid members in analysis",
                                    "value": "invalid",
                                }
                            ],
                            value=[],
                            className="include-toggle",
                        ),
                    ]
                    if invalid
                    else []
                ),
                html.Div(
                    compare_children(compare, project, record.outcome, False),
                    id={"inv-compare": "content"},
                ),
            ],
            id={"inv-region": "compare"},
            style=display if active == "compare" else hidden,
        ),
        html.Div(
            _series_region(service, project, tray, doc),
            id={"inv-region": "series"},
            style=display if active == "series" else hidden,
        ),
        html.Div(
            analysis.points_tab(service, project, tray, record.outcome),
            id={"inv-region": "points"},
            style=display if active == "points" else hidden,
        ),
        html.Div(
            search_region(
                service,
                project,
                investigation_id,
                record.members,
                scoped,
            ),
            id={"inv-region": "search"},
            style=display if active == "search" else hidden,
        ),
    ]
    return html.Div(
        [
            *([components.Error(error)] if error else []),
            investigation_crumb(
                project,
                record.name,
                _INVESTIGATION_VIEW_TITLES[active],
                member_label,
            ),
            html.H2(record.name),
            html.P(
                [
                    f"factor {record.factor} · outcome {record.outcome} · "
                    f"{counted_sweeps(detail.coverage.members)}",
                ],
                className="inv-sub",
            ),
            html.Div(
                [
                    *_views_row(active),
                    member_scope_row(member_label),
                    open_in_python(record, scoped),
                    html.A(
                        "Edit members",
                        href=f"{ROUTES_BASE}/project/{project}/investigation/"
                        f"{investigation_id}/edit",
                        className="btn-link",
                    ),
                ],
                className="inv-views",
            ),
            html.Div(id={"analysis-error": "page"}),
            *regions,
        ],
        className="page investigation",
    )


def _series_region(
    service: DashboardService,
    project: str,
    tray: dict[str, Any],
    doc: dict[str, Any],
) -> html.Div:
    """The Series tray over the investigation scope: the sweep-scope
    Series components, initialized from the page's view document."""
    now = time.time_ns()
    snapshot = analysis.series_snapshot(service, project, tray, doc, now)
    panels, _payload, key_options, color_options, facet_options, filters, status = (
        analysis.render_series_outputs(doc, snapshot)
    )
    panels, figure = analysis.extract_series_figure(panels)
    return series_tab(
        doc,
        panels,
        figure,
        key_options,
        color_options,
        facet_options,
        filters,
        status,
        analysis.updated_ago(now),
    )


_EDITOR_SWEEP_COLUMNS: list[SortColumn] = [
    SortColumn("name", "Sweep", "string"),
    SortColumn("state", "State", "string"),
    SortColumn("member", "Member", "string"),
    SortColumn("curation", "Curation", "string"),
    SortColumn("completed", "Completed", "numeric"),
    SortColumn(
        "expected_trials",
        "Expected trials",
        "numeric",
        definition={"valueFormatter": {"function": "renderMissing(x)"}},
    ),
    SortColumn(
        "last_activity_ns",
        "Last activity",
        "ns",
        definition={"valueFormatter": {"function": "renderRelative(x)"}},
    ),
]


def editor_rows(
    summaries: Sequence[SweepSummary], member_ids: Sequence[str], project: str
) -> list[dict[str, Any]]:
    """Every project sweep as an editor row; ``member`` marks saved
    membership so the checkbox (the working set) never hides a change."""
    saved = set(member_ids)
    return [
        {
            "sweep_id": summary.sweep_id,
            "name": summary.name,
            "link_label": summary.name,
            "link_href": analysis.workspace_focus_href(
                project, "sweep", summary.sweep_id
            ),
            "state": summary.state,
            "member": "member" if summary.sweep_id in saved else MISSING,
            "curation": sweep_curation(summary) or MISSING,
            "completed": summary.succeeded,
            "expected_trials": summary.expected_trials,
            "last_activity_ns": summary.latest_submitted_ns,
        }
        for summary in sorted(summaries, key=lambda row: row.name.casefold())
    ]


def _editor_grid(rows: list[dict[str, Any]]) -> AgGrid:
    """The project sweep table with row-selection checkboxes; the
    working selection arrives through the loader callback."""
    return AgGrid(
        id={"inv-edit-grid": "grid"},
        rowData=rows,
        columnDefs=sortable_columns(_EDITOR_SWEEP_COLUMNS),
        defaultColDef=_GRID_DEFAULTS,
        dashGridOptions=components.grid_options(
            pagination=True,
            paginationPageSize=_OVERVIEW_PAGE_SIZE,
            rowSelection={
                "mode": "multiRow",
                "checkboxes": True,
                "headerCheckboxSelection": True,
                "enableClickSelection": False,
            },
        ),
        getRowId=_SWEEP_ROW_ID,
        className="ag-theme-alpine grid",
    )


_FACTOR_KIND_LABELS = {
    "manual_param": "param",
    "config_source": "config source",
    "name_token": "name token",
}


def editor_factor_options(preview: InvestigationPreview) -> list[dict[str, str]]:
    """Dropdown options from the preview's factor candidates, each with
    its real member coverage."""
    return [
        {
            "label": (
                f"{_FACTOR_KIND_LABELS[candidate.kind]} {candidate.name} — "
                f"{candidate.members} of {preview.member_count} members"
            ),
            "value": candidate.name,
        }
        for candidate in preview.factors
    ]


def editor_outcome_options(preview: InvestigationPreview) -> list[dict[str, str]]:
    return [
        {
            "label": (
                f"{candidate.key} — {candidate.members} of "
                f"{preview.member_count} members"
            ),
            "value": candidate.key,
        }
        for candidate in preview.outcomes
    ]


def editor_preview_panel(preview: InvestigationPreview, state: dict) -> list[Any]:
    """The deterministic pre-save facts: candidate factors/outcomes with
    real coverage counts, the shared warnings, and the pending diff."""
    picked = list(state.get("picked") or [])
    saved = set(state.get("saved") or ())
    added = [sweep_id for sweep_id in picked if sweep_id not in saved]
    removed = [sweep_id for sweep_id in saved if sweep_id not in picked]
    pending = f"+{len(added)} -{len(removed)} (unsaved)" if added or removed else "none"
    panel: list[Any] = [
        html.P(
            [
                html.B(str(preview.member_count)),
                " project members picked · pending change: ",
                html.B(pending),
            ],
            className="overview-sub",
        )
    ]
    if preview.factors or preview.outcomes:
        factor_lines = [
            html.Div(
                f"{_FACTOR_KIND_LABELS[candidate.kind]} "
                f"{candidate.name} — {candidate.members} of "
                f"{preview.member_count} members"
            )
            for candidate in preview.factors
        ] or [html.Div("none observed")]
        outcome_lines = [
            html.Div(
                f"{candidate.key} — {candidate.members} of "
                f"{preview.member_count} members"
            )
            for candidate in preview.outcomes
        ] or [html.Div("none observed")]
        panel.append(
            html.Div(
                [
                    html.Div(
                        [html.B("Candidate factors"), *factor_lines],
                        className="preview-col",
                    ),
                    html.Div(
                        [html.B("Candidate outcomes"), *outcome_lines],
                        className="preview-col",
                    ),
                ],
                className="preview-cols",
            )
        )
    if preview.warnings:
        panel.append(
            html.Ul(
                [
                    html.Li(f"{warning.kind.replace('_', ' ')}: {warning.detail}")
                    for warning in preview.warnings
                ],
                className="preview-warnings",
            )
        )
    return panel


def investigation_edit_page(
    service: DashboardService,
    project: str,
    investigation_id: str | None,
    seed_sweeps: Sequence[str],
) -> html.Div:
    """The member editor: create (``/new``, seeded from ?sweeps=) and
    edit (``/<id>/edit``) are distinct flows — a create never overwrites
    an existing investigation, and nothing is written until Save."""
    if not project:
        return html.Div(
            components.Empty("Pick a project in the header to edit investigations.")
        )
    summaries = service.sweep_overview(project)
    if investigation_id is None:
        picked = sorted(set(seed_sweeps))
        state = {
            "picked": picked,
            "saved": [],
            "name": "",
            "factor": None,
            "outcome": None,
        }
        preview = service.investigation_preview(project, picked)
        return html.Div(
            [
                investigation_crumb(project, "New Investigation"),
                html.H2("New Investigation"),
                html.P(
                    "Drafts stay local until Save; a name this project's "
                    "investigations already use cannot be created again with "
                    "a different body.",
                    className="inv-sub",
                ),
                html.Div(
                    [
                        dcc.Input(
                            id={"inv-edit-name": "name"},
                            value="",
                            placeholder="Investigation name…",
                            className="quick-filter inv-name",
                        ),
                        dcc.Dropdown(
                            id={"inv-edit-factor": "factor"},
                            options=editor_factor_options(preview),
                            placeholder="Comparison factor…",
                            className="inv-dd",
                        ),
                        dcc.Dropdown(
                            id={"inv-edit-outcome": "outcome"},
                            options=editor_outcome_options(preview),
                            placeholder="Outcome key…",
                            className="inv-dd",
                        ),
                    ],
                    className="inv-create-controls",
                ),
                *_editor_body(project, summaries, state, preview, editing=False),
            ],
            className="page investigation-edit",
        )
    detail = service.investigation_detail(investigation_id)
    record = detail.investigation
    members = [str(sweep) for sweep in record.members]
    state = {
        "picked": list(members),
        "saved": list(members),
        "name": record.name,
        "factor": record.factor,
        "outcome": record.outcome,
    }
    preview = service.investigation_preview(project, members)
    return html.Div(
        [
            investigation_crumb(project, record.name, "Edit members"),
            html.H2("Edit members"),
            html.P(
                f"{record.name} · factor {record.factor} · outcome "
                f"{record.outcome} — the name, factor, and outcome are "
                "fixed; membership edits save explicitly.",
                className="inv-sub",
            ),
            *_editor_body(project, summaries, state, preview, editing=True),
        ],
        className="page investigation-edit",
    )


def _editor_body(
    project: str,
    summaries: Sequence[SweepSummary],
    state: dict,
    preview: InvestigationPreview,
    *,
    editing: bool,
) -> list[Any]:
    """Controls shared by the create and edit flows: the deterministic
    preview, the project sweep table with the working selection, and
    the save row."""
    rows = editor_rows(summaries, state["saved"], project)
    return [
        dcc.Store(id={"inv-edit-state": "members"}, data=state),
        html.Div(
            editor_preview_panel(preview, state),
            id={"inv-edit-preview": "preview"},
            className="inv-preview",
        ),
        html.Section(
            [
                html.H3("Project sweeps"),
                _editor_grid(rows),
                html.P(
                    "Checkboxes edit the working member set; the Member "
                    "column shows what is saved. Selection never mutates "
                    "membership directly.",
                    className="hint",
                ),
            ],
            className="section editor-sweeps",
        ),
        html.Div(
            [
                html.Button(
                    "Save members" if editing else "Create investigation",
                    id={"inv-edit-save": "save"},
                    n_clicks=0,
                    disabled=not editing,
                    className="btn-primary",
                ),
                *(
                    [
                        html.Button(
                            "Discard changes",
                            id={"inv-edit-discard": "discard"},
                            n_clicks=0,
                            className="btn-link",
                        )
                    ]
                    if editing
                    else []
                ),
                html.Span(id={"inv-edit-message": "message"}),
            ],
            className="inv-save-row",
        ),
        html.P(
            html.A(
                f"Back to {project} investigations",
                href=investigations_index_href(project),
            ),
            className="hint",
        ),
    ]


def series_tab(
    doc: dict[str, Any],
    panels: list[Any],
    figure: Any,
    key_options: list[dict[str, str]],
    color_options: list[dict[str, Any]],
    facet_options: list[dict[str, str]],
    filters: list[Any],
    status: str,
    updated: str,
) -> html.Div:
    """The Series tray over the investigation scope: per-metric
    visibility, median+IQR band vs raw traces, and focus as
    highlight-only — the same trajectory semantics as the sweep-scope
    Series, mounted with the page's current state."""
    series = doc["series"]
    return html.Div(
        [
            html.Div(
                [
                    dcc.Dropdown(
                        id="analysis-key",
                        placeholder="Value keys…",
                        multi=True,
                        searchable=True,
                        options=key_options,
                        value=list(series["keys"]),
                    ),
                    dcc.RadioItems(
                        id="analysis-mode",
                        options=[
                            {"label": " Stacked", "value": "stacked"},
                            {"label": " Overlay", "value": "overlay"},
                        ],
                        value=series["mode"],
                        inline=True,
                    ),
                    dcc.Dropdown(
                        id="analysis-color",
                        placeholder="Color by…",
                        options=color_options,
                        value=series["color"],
                    ),
                    dcc.Dropdown(
                        id="analysis-facet",
                        placeholder="Facet rows…",
                        options=facet_options,
                        value=series["facet"],
                    ),
                    dcc.RadioItems(
                        id="analysis-reduction",
                        options=[
                            {"label": f" {name}", "value": name}
                            for name in analysis.ANALYSIS_REDUCTIONS
                        ],
                        value=series["reduction"],
                        inline=True,
                    ),
                ],
                className="analysis-controls",
            ),
            html.Div(
                [
                    dcc.RadioItems(
                        id="analysis-display",
                        options=[
                            {"label": " All raw", "value": "all"},
                            {"label": " Highlighted only", "value": "highlighted"},
                            {"label": " Median + IQR", "value": "median_iqr"},
                        ],
                        value=series["trial_display"],
                        inline=True,
                    ),
                    html.Button(
                        "Refresh",
                        id={"analysis-refresh": "series"},
                        n_clicks=0,
                    ),
                    dcc.Checklist(
                        id="analysis-auto-refresh",
                        options=[
                            {
                                "label": " Auto-refresh while incomplete",
                                "value": "auto",
                            }
                        ],
                        value=["auto"] if doc["auto_refresh"] else [],
                        inline=True,
                    ),
                    html.Span(
                        id="analysis-series-status",
                        className="series-status",
                        children=status,
                    ),
                    html.Span(
                        id="analysis-updated",
                        className="series-updated",
                        children=updated,
                    ),
                ],
                className="series-toolbar",
            ),
            html.P(
                [
                    "Display mode sets how trials compare; reduction "
                    "folds executions within each trial.",
                    html.Span(
                        "?",
                        title=(
                            "All raw renders every series (line density "
                            "warns above 100). Highlighted only renders "
                            "the highlighted trials — a trace click "
                            "highlights and focuses that trial. Median + "
                            "IQR aggregates per color/facet group at each "
                            "observed step. Reduction “none” shows every "
                            "(trial, execution) series as logged; "
                            "mean/min/max fold executions within each "
                            "trial."
                        ),
                        className="help",
                    ),
                ],
                className="hint",
            ),
            html.Div(
                filters,
                id="analysis-context-filters",
                className="context-filters",
            ),
            html.Div(panels, id="analysis-series-panels"),
            dcc.Graph(
                id="analysis-series-figure",
                figure=figure,
                clear_on_unhover=True,
            ),
            dcc.Store(id="analysis-series-figure-store", data=figure),
            dcc.Store(id="analysis-series-data"),
            dcc.Store(id={"analysis-refresh-store": "series"}),
        ]
    )


_SEARCH_COLUMNS: list[SortColumn] = [
    SortColumn(
        "name",
        "Sweep",
        "string",
        definition={"cellRenderer": {"function": "renderLinkCell(params)"}},
    ),
    SortColumn("state", "State", "string"),
    SortColumn("completed", "Completed", "numeric"),
    SortColumn(
        "expected_trials",
        "Expected trials",
        "numeric",
        definition={"valueFormatter": {"function": "renderMissing(x)"}},
    ),
    SortColumn(
        "last_activity_ns",
        "Last activity",
        "ns",
        definition={"valueFormatter": {"function": "renderRelative(x)"}},
    ),
]


def search_rows(
    summaries: Sequence[SweepSummary],
    project: str,
    investigation_id: str,
) -> list[dict[str, Any]]:
    """One Search row per member sweep; the link opens the sweep hub
    carrying the investigation return path."""

    def hub_href(sweep_id: str) -> str:
        doc = analysis.with_focus(
            analysis.default_view_state(), {"kind": "sweep", "id": sweep_id}
        )
        doc = analysis.edited_view(doc, {"via": investigation_id})
        return f"{ROUTES_BASE}/project/{project}?view={analysis.encode_view_state(doc)}"

    return [
        {
            "sweep_id": summary.sweep_id,
            "name": summary.name,
            "link_href": hub_href(summary.sweep_id),
            "link_label": summary.name,
            "state": summary.state,
            "completed": summary.succeeded,
            "expected_trials": summary.expected_trials,
            "last_activity_ns": summary.latest_submitted_ns,
        }
        for summary in sorted(summaries, key=lambda row: row.name.casefold())
    ]


def search_region(
    service: DashboardService,
    project: str,
    investigation_id: str,
    members: Sequence[Any] | None,
    member: str | None = None,
) -> html.Div:
    """The minimal member filter: a debounced name filter over the
    member sweeps only (the scoped member alone when narrowed); a
    sweep's link opens the hub carrying the investigation return
    path."""
    member_ids = {str(sweep) for sweep in members or ()}
    if member and member in member_ids:
        member_ids = {member}
    rows = search_rows(
        [
            summary
            for summary in service.sweep_overview(project)
            if summary.sweep_id in member_ids
        ],
        project,
        investigation_id,
    )
    return html.Div(
        [
            html.P(
                "Search covers the investigation's members only; the "
                "project's other sweeps are out of scope here.",
                className="hint",
            ),
            html.Div(
                [
                    dcc.Input(
                        id="inv-search-q",
                        type="search",
                        placeholder="Filter member sweeps…",
                        className="quick-filter",
                        debounce=True,
                    ),
                    html.Span(id="inv-search-note", className="series-status"),
                ],
                className="analysis-controls",
            ),
            AgGrid(
                id="inv-search-grid",
                rowData=rows,
                columnDefs=sortable_columns(_SEARCH_COLUMNS),
                defaultColDef=_GRID_DEFAULTS,
                dashGridOptions=components.grid_options(),
                getRowId=_SWEEP_ROW_ID,
                className="ag-theme-alpine grid",
            ),
        ],
        className="section",
    )
