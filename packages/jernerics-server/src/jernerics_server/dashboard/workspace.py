import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qs, quote
from uuid import UUID

from dash import dcc, html
from dash.development.base_component import Component
from jernerics_schema import (
    InvestigationRecord,
    Selection,
    encode_selection,
    materialize_selection,
)

from . import analysis, components, figures, page
from .components import MISSING, short_id
from .render import SortColumn, sort_rows
from .routes import ROUTES_BASE
from .service import (
    CompareDocument,
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
    InvestigationPreview,
    InvestigationRow,
    SweepSummary,
)

INVESTIGATION_VIEWS = analysis.INVESTIGATION_VIEWS


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


def investigations_index_href(project: str) -> str:
    """The investigations index URL for one project."""
    return f"{ROUTES_BASE}/project/{quote(project, safe='')}/investigations"


def investigation_crumb(
    project: str,
    name: str,
    view: str = "compare",
    member_label: str | None = None,
) -> html.Div:
    """``project › Investigations › name › view › member``; the
    trailing view and member segments appear only when they narrow the
    page beyond Compare over all members."""
    crumbs: list[tuple[str, str] | str] = [
        (project, f"{ROUTES_BASE}/project/{quote(project, safe='')}"),
        ("Investigations", investigations_index_href(project)),
        name,
    ]
    if view != "compare":
        crumbs.append(INVESTIGATION_VIEW_LABELS[view])
    if member_label:
        crumbs.append(member_label)
    return page.breadcrumbs(crumbs)


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


def compare_empty_state(doc: CompareDocument, include_invalid: bool) -> html.Section:
    """The honest empty state: an analysis set with nothing to compare
    names exactly who is excluded and why."""
    members_total = len(doc.members)
    no_outcome = members_total - len(doc.analyzable)
    if doc.analyzable:
        return html.Section(
            html.P(
                f"{no_outcome} of {members_total} members have no outcome "
                "data; the rest are compared below."
            ),
            className="section",
        )
    if include_invalid:
        return html.Section(
            html.P(
                f"None of the {members_total} members has outcome data — "
                "nothing to compare."
            ),
            className="section",
        )
    return html.Section(
        html.P(
            "No analyzable members in the analysis set — "
            f"{doc.excluded_data_bearing} data-bearing members are marked "
            "invalid (excluded by default) and "
            f"{no_outcome - doc.excluded_data_bearing} have no outcome data. "
            "Tick “include invalid members in analysis” to see the real "
            "comparison."
        ),
        className="section",
    )


def investigation_page(
    service: DashboardService,
    project: str,
    investigation_id: str,
    search: str | None = None,
    now_ns: int | None = None,
) -> html.Div:
    """The Investigation workspace on the new shell: crumbs, header,
    the view row, and one view per page — Compare, Series, Points,
    Search, or Python. The plain query string carries the view, the
    member scope, and the Compare include-invalid flag; an unknown
    member falls back to all members and Compare never poses as
    scoped."""
    now = time.time_ns() if now_ns is None else now_ns
    if not project or not investigation_id:
        return page.page_shell(
            "Investigations",
            project,
            components.Empty("No investigation requested."),
            scope="Investigation",
        )
    try:
        detail = service.investigation_detail(investigation_id)
        record = detail.investigation
        query = investigation_query(search)
        view = query["view"]
        tray, scoped = analysis.investigation_scope_state(
            record.members, query["member"]
        )
        if view == "compare":
            compare = service.investigation_compare(
                investigation_id, include_invalid=query["include_invalid"]
            )
    except CurationUnavailableError as error:
        return page.page_shell(
            "Investigations",
            project,
            components.Empty(str(error)),
            scope="Investigation",
        )
    member_label = None
    if scoped:
        focused = service.sweep_detail(scoped)
        member_label = focused.overview.name if focused else short_id(scoped)
    h1 = (
        record.name
        if view == "compare"
        else (f"{record.name} — {INVESTIGATION_VIEW_LABELS[view]}")
    )
    if member_label and view in ("series", "points", "search"):
        h1 = f"{h1} — {member_label}"
    header: list[Any] = [
        investigation_crumb(project, record.name, view, member_label),
        html.H1(h1),
        _inv_nav_row(
            project,
            investigation_id,
            view,
            scoped,
            query["include_invalid"],
            query["q"],
        ),
    ]
    drop_href = investigation_url(
        project,
        investigation_id,
        view=view,
        include_invalid=query["include_invalid"],
        q=query["q"],
    )
    if view == "compare":
        keys = ", ".join(compare.signature_keys)
        if not keys:
            keys = "none observed"
        body: list[Any] = [
            html.P(
                [
                    "factor ",
                    html.B(record.factor),
                    " · outcome ",
                    html.B(record.outcome),
                    " (final) · matching by exact sampled signature "
                    f"({keys}) · no imputation, no outliers suppressed",
                ],
                className="sub",
            ),
            *compare_body(
                compare,
                project,
                record.outcome,
                investigation_id,
                query["include_invalid"],
            ),
        ]
        wide = False
    elif view == "series":
        body = [
            html.P(
                "trials of the member cohort with fetched series · same "
                "trajectory semantics as sweep-scoped Series",
                className="sub",
            ),
            *series_body(service, project, record, tray, member_label, drop_href),
        ]
        wide = True
    elif view == "points":
        body = [
            html.P(
                "member trials × tracked scalars (final logged value) · "
                f"params → {record.outcome} selection below",
                className="sub",
            ),
            *points_body(service, project, record, tray, member_label, drop_href),
        ]
        wide = True
    elif view == "search":
        body = [
            html.P(
                "Search covers the investigation's members only; the "
                "project's other sweeps are out of scope here.",
                className="sub",
            ),
            *search_body(
                service,
                project,
                record,
                scoped,
                member_label,
                drop_href,
                query["q"],
                now,
            ),
        ]
        wide = False
    else:
        scope_note = f", narrowed to member {member_label}" if member_label else ""
        body = [
            html.P(
                "The exact effective Selection — the investigation's "
                f"persisted members{scope_note}.",
                className="sub",
            ),
            *python_body(record, scoped),
        ]
        wide = False
    return page.page_shell(
        "Investigations",
        project,
        *header,
        *body,
        wide=wide,
        scope="Investigation",
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
    now_ns: int | None = None,
) -> html.Div:
    """The member editor: create (``/new``, seeded from ?sweeps=) and
    edit (``/<id>/edit``) are distinct flows — a create never overwrites
    an existing investigation, and nothing is written until Save."""
    now = time.time_ns() if now_ns is None else now_ns
    crumbs = [
        (project, f"{ROUTES_BASE}/project/{quote(project, safe='')}"),
        ("Investigations", investigations_index_href(project)),
    ]
    try:
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
            body: list[Any] = [
                page.breadcrumbs([*crumbs, "New Investigation"]),
                html.H1("New Investigation"),
                html.P(
                    "Drafts stay local until Save; a name this project's "
                    "investigations already use cannot be created again with "
                    "a different body.",
                    className="sub",
                ),
                page.limit_row(
                    html.Span("Name", className="annotate"),
                    dcc.Input(
                        id={"inv-edit-name": "name"},
                        value="",
                        placeholder="Investigation name…",
                        className="inv-name",
                    ),
                ),
                page.limit_row(
                    html.Span("Comparison factor", className="annotate"),
                    dcc.Dropdown(
                        id={"inv-edit-factor": "factor"},
                        options=editor_factor_options(preview),
                        placeholder="Comparison factor…",
                        className="inv-dd",
                    ),
                ),
                page.limit_row(
                    html.Span("Outcome key", className="annotate"),
                    dcc.Dropdown(
                        id={"inv-edit-outcome": "outcome"},
                        options=editor_outcome_options(preview),
                        placeholder="Outcome key…",
                        className="inv-dd",
                    ),
                ),
                *_editor_body(
                    project, summaries, state, preview, editing=False, now_ns=now
                ),
            ]
            return page.page_shell(
                "Investigations", project, *body, scope="Investigation"
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
        body = [
            page.breadcrumbs([*crumbs, record.name, "Edit members"]),
            html.H1("Edit members"),
            html.P(
                f"{record.name} · factor {record.factor} · outcome "
                f"{record.outcome} — the name, factor, and outcome are "
                "fixed; membership edits save explicitly.",
                className="sub",
            ),
            *_editor_body(project, summaries, state, preview, editing=True, now_ns=now),
        ]
    except CurationUnavailableError as error:
        return page.page_shell(
            "Investigations",
            project,
            components.Empty(str(error)),
            scope="Investigation",
        )
    return page.page_shell("Investigations", project, *body, scope="Investigation")


def _editor_body(
    project: str,
    summaries: Sequence[SweepSummary],
    state: dict,
    preview: InvestigationPreview,
    *,
    editing: bool,
    now_ns: int,
) -> list[Any]:
    """Controls shared by the create and edit flows: the deterministic
    preview, the working-set seg and save row, and the project sweep
    table with a checkbox per sweep."""
    seg = html.Div(
        [
            html.A(
                f"All sweeps ({len(summaries)})",
                id={"inv-edit-mode": "all"},
                className="on",
            ),
            html.A(
                f"Members ({len(state['picked'])})",
                id={"inv-edit-mode": "members"},
            ),
        ],
        className="seg",
    )
    return [
        dcc.Store(id={"inv-edit-state": "members"}, data=state),
        html.Div(
            editor_preview_panel(preview, state),
            id={"inv-edit-preview": "preview"},
            className="inv-preview",
        ),
        page.limit_row(
            seg,
            html.Span(className="spacer"),
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
                        className="btn",
                    )
                ]
                if editing
                else []
            ),
            html.Span(id={"inv-edit-message": "message"}),
        ),
        _editor_table(summaries, state["picked"], project, now_ns),
        html.P(
            "Checkboxes edit the working member set; the seg narrows the "
            "view to the current picks. Selection never mutates "
            "membership directly.",
            className="annotate",
        ),
        html.P(
            html.A(
                f"Back to {project} investigations",
                href=investigations_index_href(project),
            ),
            className="annotate",
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


def search_rows(
    summaries: Sequence[SweepSummary],
    project: str,
    investigation_id: str,
    needle: str,
    now_ns: int,
) -> tuple[list[html.Tr], int, int]:
    """(rows, shown, total) for the member filter; the sweep links
    carry the investigation return path."""
    ordered = sorted(summaries, key=lambda row: row.name.casefold())
    shown = [
        summary
        for summary in ordered
        if not needle or needle in summary.name.casefold()
    ]
    rows = [
        html.Tr(
            [
                html.Td(
                    html.A(
                        summary.name,
                        href=sweep_page_url(
                            project, summary.sweep_id, investigation_id
                        ),
                    )
                ),
                html.Td(summary.state),
                html.Td(
                    _fraction(summary.succeeded, summary.expected_trials),
                    className="num",
                ),
                html.Td(_rel(summary.latest_submitted_ns, now_ns), className="num"),
            ]
        )
        for summary in shown
    ]
    return rows, len(shown), len(ordered)


def investigation_coverage_text(row: InvestigationRow) -> str:
    """The one-line coverage summary: with outcome / incomplete / invalid."""
    incomplete = row.member_count - row.completed
    return (
        f"{row.with_outcome} with outcome · {incomplete} incomplete · "
        f"{row.invalid} invalid"
    )


INVESTIGATION_VIEW_LABELS: dict[str, str] = {
    "compare": "Compare",
    "series": "Series",
    "points": "Points",
    "search": "Search",
    "python": "Python",
}


def _editor_url(project: str, investigation_id: str | None) -> str:
    """The member editor URL: ``/new`` for a create, ``/<id>/edit`` otherwise."""
    base = f"{ROUTES_BASE}/project/{quote(project, safe='')}/investigation"
    if investigation_id is None:
        return f"{base}/new"
    return f"{base}/{investigation_id}/edit"


def sweep_page_url(project: str, sweep_id: str, via: str | None = None) -> str:
    """The sweep page URL; ``?via=`` names the investigation that
    linked here so the sweep page can offer the way back."""
    url = f"{ROUTES_BASE}/project/{quote(project, safe='')}/sweep/{sweep_id}"
    return f"{url}?via={via}" if via else url


def investigation_search(
    view: str = "compare",
    member: str | None = None,
    include_invalid: bool = False,
    q: str | None = None,
) -> str:
    """The plain query string of an investigation page; Compare is the
    default and never names itself."""
    params: list[str] = []
    if view != "compare":
        params.append(f"view={view}")
    if member:
        params.append(f"member={quote(member, safe='')}")
    if include_invalid:
        params.append("include-invalid=1")
    if q:
        params.append(f"q={quote(q, safe='')}")
    return f"?{'&'.join(params)}" if params else ""


def investigation_url(
    project: str,
    investigation_id: str,
    view: str = "compare",
    member: str | None = None,
    include_invalid: bool = False,
    q: str | None = None,
) -> str:
    """One investigation page URL."""
    base = (
        f"{ROUTES_BASE}/project/{quote(project, safe='')}"
        f"/investigation/{investigation_id}"
    )
    return base + investigation_search(view, member, include_invalid, q)


def investigation_query(search: str | None) -> dict[str, Any]:
    """The plain query state of an investigation page: the active view
    (unknown names fall back to Compare), the requested member scope
    (callers resolve it against the membership), the Compare
    include-invalid flag, and the Search filter text."""
    params = parse_qs((search or "").lstrip("?"))

    def first(key: str) -> str:
        return (params.get(key) or [""])[0]

    view = first("view")
    return {
        "view": view if view in INVESTIGATION_VIEW_LABELS else "compare",
        "member": first("member") or None,
        "include_invalid": first("include-invalid") == "1",
        "q": first("q"),
    }


def _rel(ns: int | None, now_ns: int) -> str:
    """The coarse relative age the tables show."""
    if not ns:
        return "never"
    delta = max(0, (now_ns - ns) // 1_000_000_000)
    if delta < 90:
        return f"{delta}s ago"
    if delta < 5400:
        return f"{delta // 60}m ago"
    if delta < 172800:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def _fraction(done: int, total: int | None) -> str:
    if total:
        return f"{done}/{total}"
    return str(done) if done else MISSING


def _meta_strip(cells: Sequence[tuple[str, Any]]) -> html.Div:
    """The fact strip: label/value pairs in a meta grid."""
    return html.Div(
        [html.Div([html.Span(label), html.B(value)]) for label, value in cells],
        className="meta-grid",
    )


def _curation_badges(*flags: tuple[bool, str]) -> list[html.Span]:
    """The invalid/archived badge spans for the flags that hold."""
    return [html.Span(name, className=f"badge {name}") for flag, name in flags if flag]


def investigations_index_page(
    service: DashboardService, project: str, now_ns: int
) -> html.Div:
    """The Investigations index: one row per investigation with its
    real coverage facts, and the project's Unorganized sweeps."""
    try:
        index_rows = service.investigations_index(project)
        unorganized = service.unorganized(project)
    except CurationUnavailableError as error:
        return page.page_shell(
            "Investigations",
            project,
            components.Empty(str(error)),
            scope="Project",
        )
    head = [
        page.head_cell("Investigation"),
        page.head_cell("Factor"),
        page.head_cell("Outcome"),
        page.head_cell("Members", numeric=True),
        page.head_cell("Coverage"),
        page.head_cell("Last activity", numeric=True),
        page.head_cell(""),
    ]
    rows = [
        html.Tr(
            [
                html.Td(
                    html.A(
                        html.Span(row.name, className="sfx"),
                        href=investigation_url(project, row.investigation_id),
                        className="sweep-link",
                    )
                ),
                html.Td(row.factor),
                html.Td(row.outcome),
                html.Td(str(row.member_count), className="num"),
                html.Td(investigation_coverage_text(row)),
                html.Td(_rel(row.last_activity_ns, now_ns), className="num"),
                html.Td(
                    html.A(
                        "Edit members",
                        href=_editor_url(project, row.investigation_id),
                    )
                ),
            ]
        )
        for row in index_rows
    ]
    unorganized_rows = [
        html.Tr(
            [
                html.Td(
                    html.A(summary.name, href=sweep_page_url(project, summary.sweep_id))
                ),
                html.Td(summary.state),
                html.Td(
                    _fraction(summary.succeeded, summary.expected_trials),
                    className="num",
                ),
                html.Td(_rel(summary.latest_submitted_ns, now_ns), className="num"),
            ]
        )
        for summary in unorganized
    ]
    body: list[Any] = [
        html.H1("Investigations"),
        html.P(
            f"Cross-sweep questions over {project} · membership is server-persisted",
            className="sub",
        ),
        html.Div(
            html.A(
                "New Investigation",
                href=_editor_url(project, None),
                className="btn btn-primary",
            ),
            className="actions",
        ),
        html.Section(
            (
                [html.Table([html.Thead(html.Tr(head)), html.Tbody(rows)])]
                if index_rows
                else [
                    components.Empty(
                        "No investigations yet — pick sweeps in Overview and "
                        "use Create Investigation, or start a new one."
                    )
                ]
            ),
            className="section investigations-index",
        ),
        html.H2("Unorganized"),
        html.P(
            f"{counted_sweeps(len(unorganized))} not in any Investigation",
            className="sub",
        ),
    ]
    if unorganized:
        body.append(
            html.Details(
                [
                    html.Summary("Show list"),
                    page.scroll_table(
                        [
                            page.head_cell("Sweep"),
                            page.head_cell("Status"),
                            page.head_cell("Trials", numeric=True),
                            page.head_cell("Last activity", numeric=True),
                        ],
                        unorganized_rows,
                        sortable=True,
                    ),
                ],
                className="failgroup",
            )
        )
    return page.page_shell("Investigations", project, *body, scope="Project")


def _inv_nav_row(
    project: str,
    investigation_id: str,
    view: str,
    member: str | None,
    include_invalid: bool,
    q: str,
) -> html.Div:
    """The view switcher with its Python and Edit actions; Compare
    never carries a member scope, the filter text stays on Search."""
    views = [
        (
            INVESTIGATION_VIEW_LABELS[name],
            investigation_url(
                project,
                investigation_id,
                view=name,
                member=member if name != "compare" else None,
                include_invalid=include_invalid,
                q=q if name == "search" else None,
            ),
        )
        for name in INVESTIGATION_VIEWS
    ]
    return page.inv_nav(
        INVESTIGATION_VIEW_LABELS[view],
        views,
        python_href=investigation_url(
            project, investigation_id, view="python", member=member
        ),
        edit_href=_editor_url(project, investigation_id),
    )


def _scope_note_row(member_label: str | None, drop_href: str) -> html.Div:
    """The member-scope line: the visible scope fact plus the one-click
    way back to the full cohort (hidden while unscoped)."""
    return page.limit_row(
        html.Span(
            f"Scoped to member {member_label}" if member_label else "",
            id="inv-member-note",
            className="annotate",
        ),
        html.A(
            "All members",
            href=drop_href,
            className="btn",
            id="inv-member-clear",
            style={} if member_label else {"display": "none"},
        ),
    )


def python_body(record: InvestigationRecord, member: str | None = None) -> list[Any]:
    """The Open in Python view: the exact effective Selection as an
    opaque token plus the runnable handoff snippet."""
    selection = materialize_selection(record)
    if member:
        selection = Selection(project=record.project, sweeps=(UUID(member),))
    token = encode_selection(selection)
    snippet = analysis.python_snippet(token, record.project, "http://localhost:8000")
    style = {"whiteSpace": "pre-wrap", "overflowX": "auto"}
    return [
        html.Div(
            [
                html.Span("Copy the token or the snippet:", className="annotate"),
                dcc.Clipboard(content=token, title="Copy selection token"),
                dcc.Clipboard(content=snippet, title="Copy runnable snippet"),
            ],
            className="actions",
        ),
        html.Section(
            html.Pre(token, className="config-json", style=style),
            className="section",
        ),
        html.Section(
            html.Pre(snippet, className="config-json", style=style),
            className="section",
        ),
        html.P(
            "The token decodes to the exact effective membership via "
            "jernerics_schema.decode_selection; point TrackingClient at "
            "your server.",
            className="annotate",
        ),
    ]


def _sig_text(value: float | None) -> str:
    return MISSING if value is None else f"{value:.4g}"


def _matched_table(doc: CompareDocument, labels: dict[str, str]) -> html.Table:
    """Signatures matched by two or more analyzable members; one
    numeric column per member, missing stays missing."""
    head = [page.head_cell("Signature")] + [
        page.head_cell(labels.get(sweep_id, short_id(sweep_id)), numeric=True)
        for sweep_id in doc.analyzable
    ]
    rows = []
    for row in doc.signatures:
        if row.matched < 2:
            continue
        chip = (
            [html.Span("common", className="count-badge neutral")] if row.common else []
        )
        rows.append(
            html.Tr(
                [html.Td([row.label, *chip], className="mono")]
                + [
                    html.Td(_sig_text(row.values.get(sweep_id)), className="num")
                    for sweep_id in doc.analyzable
                ]
            )
        )
    return html.Table([html.Thead(html.Tr(head)), html.Tbody(rows)])


def _members_table(
    doc: CompareDocument, project: str, investigation_id: str
) -> html.Table:
    """The member inventory: factor, the sweep link (carrying the
    ``?via=`` return path), status, completed, and usable trials."""
    rows = [
        html.Tr(
            [
                html.Td(member.factor_value or MISSING),
                html.Td(
                    [
                        html.A(
                            member.name,
                            href=sweep_page_url(
                                project, member.sweep_id, investigation_id
                            ),
                        ),
                        *_curation_badges(
                            (member.invalid, "invalid"), (member.archived, "archived")
                        ),
                    ]
                ),
                html.Td(member.state),
                html.Td(
                    _fraction(member.completed, member.expected_trials),
                    className="num",
                ),
                html.Td(
                    _fraction(member.usable, member.expected_trials),
                    className="num",
                ),
            ]
        )
        for member in doc.members
    ]
    head = [
        page.head_cell("Factor"),
        page.head_cell("Sweep"),
        page.head_cell("Status"),
        page.head_cell("Completed", numeric=True),
        page.head_cell("Usable trials", numeric=True),
    ]
    return html.Table([html.Thead(html.Tr(head)), html.Tbody(rows)])


def compare_body(
    doc: CompareDocument,
    project: str,
    outcome: str,
    investigation_id: str,
    include_invalid: bool,
) -> list[Any]:
    """The Compare view for one analysis set: the coverage strip, the
    include-invalid toggle, the honest empty states, the charts over
    the common signatures, the matched-signature table, and the member
    inventory. No analyzable members or no global overlap each render
    their honest state instead of a manufactured ranking."""
    members = {member.sweep_id: member for member in doc.members}
    labels = {
        sweep_id: member.factor_value or member.name
        for sweep_id, member in members.items()
    }
    common = [row for row in doc.signatures if row.common]
    invalid = sum(1 for member in doc.members if member.invalid)
    body: list[Any] = [coverage_strip(doc)]
    if invalid:
        body.append(
            page.limit_row(
                html.Label(
                    [
                        dcc.Checklist(
                            id="inv-include-invalid",
                            options=[
                                {
                                    "label": " Include invalid members in analysis",
                                    "value": "invalid",
                                }
                            ],
                            value=["invalid"] if include_invalid else [],
                            className="include-toggle",
                        ),
                        html.Span(
                            f"{invalid} members marked invalid",
                            className="annotate",
                        ),
                    ]
                )
            )
        )
    if not doc.analyzable:
        body.append(compare_empty_state(doc, include_invalid))
    elif not common:
        body.append(
            html.Section(
                html.P(
                    "No sampled signature is completed by all "
                    f"{len(doc.analyzable)} analyzable members — no global "
                    "overlap. Pairwise matches are listed below; no ranking "
                    "is manufactured."
                ),
                className="section",
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
                        html.H2("Outcome heatmap"),
                        html.P(
                            "factor by exact sampled signature "
                            f"({keys}) — no imputation, no outliers "
                            "suppressed.",
                            className="annotate",
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
                        html.H2("Median over common signatures"),
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
    shared = [row for row in doc.signatures if row.matched >= 2]
    if shared:
        common_count = sum(1 for row in shared if row.common)
        body.append(
            html.Section(
                [
                    html.H2(f"Matched comparison ({outcome})"),
                    html.P(
                        f"{len(shared)} signatures matched by ≥2 analyzable "
                        f"members · {common_count} common to all "
                        f"{len(doc.analyzable)} · medians pool "
                        "matched trials; no imputation, no outliers "
                        "suppressed.",
                        className="annotate",
                    ),
                    _matched_table(doc, labels),
                ],
                className="section compare-matched",
            )
        )
    body.append(
        html.Section(
            [html.H2("Members"), _members_table(doc, project, investigation_id)],
            className="section compare-members",
        )
    )
    return body


def series_body(
    service: DashboardService,
    project: str,
    record: InvestigationRecord,
    tray: dict[str, Any],
    member_label: str | None,
    drop_href: str,
) -> list[Any]:
    """The Series view: the fact strip, the member-scope line, and the
    sweep-scope Series machinery over the investigation scope."""
    now = time.time_ns()
    doc = analysis.default_view_state()
    snapshot = analysis.series_snapshot(service, project, tray, doc, now)
    panels, _payload, key_options, color_options, facet_options, filters, status = (
        analysis.render_series_outputs(doc, snapshot)
    )
    panels, figure = analysis.extract_series_figure(panels)
    return [
        _meta_strip(
            [
                ("Members", len(tray["sweeps"])),
                ("Trials", len(snapshot["trials"])),
                ("Factor", record.factor),
                ("Outcome", record.outcome),
            ]
        ),
        _scope_note_row(member_label, drop_href),
        series_tab(
            doc,
            panels,
            figure,
            key_options,
            color_options,
            facet_options,
            filters,
            status,
            analysis.updated_ago(now),
        ),
    ]


def points_body(
    service: DashboardService,
    project: str,
    record: InvestigationRecord,
    tray: dict[str, Any],
    member_label: str | None,
    drop_href: str,
) -> list[Any]:
    """The Points view: the fact strip, the member-scope line, and the
    trials × final-scalars table with its params → outcome plot."""
    return [
        _meta_strip(
            [
                ("Members", len(tray["sweeps"])),
                ("Factor", record.factor),
                ("Outcome", record.outcome),
            ]
        ),
        _scope_note_row(member_label, drop_href),
        analysis.points_tab(service, project, tray, record.outcome),
    ]


def _editor_table(
    summaries: Sequence[SweepSummary],
    picked: Sequence[str],
    project: str,
    now_ns: int,
) -> html.Div:
    """The project sweep table with a working checkbox per row; the
    boxes start at the saved membership."""
    saved = set(picked)
    rows = []
    for summary in sorted(summaries, key=lambda row: row.name.casefold()):
        sweep_id = str(summary.sweep_id)
        rows.append(
            html.Tr(
                [
                    html.Td(
                        dcc.Checklist(
                            id={"inv-edit-pick": sweep_id},
                            options=[{"label": "", "value": sweep_id}],
                            value=[sweep_id] if sweep_id in saved else [],
                            className="pick",
                        ),
                        className="selbox",
                    ),
                    html.Td(
                        [
                            html.A(
                                summary.name,
                                href=sweep_page_url(project, sweep_id),
                            ),
                            *_curation_badges(
                                (summary.invalid, "invalid"),
                                (summary.archived, "archived"),
                            ),
                        ]
                    ),
                    html.Td(page.status_dot(summary.state)),
                    html.Td(
                        _fraction(summary.succeeded, summary.expected_trials),
                        className="num",
                    ),
                    html.Td(
                        _rel(summary.latest_submitted_ns, now_ns),
                        className="num",
                    ),
                ],
                id={"inv-edit-row": sweep_id},
            )
        )
    return page.scroll_table(
        [
            html.Th("", className="selbox"),
            page.head_cell("Sweep"),
            page.head_cell("Status"),
            page.head_cell("Trials", numeric=True),
            page.head_cell("Last activity", numeric=True),
        ],
        rows,
        sortable=True,
    )


def search_body(
    service: DashboardService,
    project: str,
    record: InvestigationRecord,
    scoped: str | None,
    member_label: str | None,
    drop_href: str,
    q: str,
    now_ns: int,
) -> list[Any]:
    """The member filter: a debounced name filter over the member
    sweeps only (the scoped member alone when narrowed); a sweep's
    link opens the sweep page carrying the investigation return path."""
    member_ids = {str(sweep) for sweep in record.members}
    if scoped and str(scoped) in member_ids:
        member_ids = {str(scoped)}
    summaries = [
        summary
        for summary in service.sweep_overview(project)
        if summary.sweep_id in member_ids
    ]
    rows, shown, total = search_rows(
        summaries, project, str(record.id), q.strip().casefold(), now_ns
    )
    return [
        _scope_note_row(member_label, drop_href),
        page.limit_row(
            dcc.Input(
                id="inv-search-q",
                type="search",
                placeholder="Filter member sweeps…",
                value=q,
                debounce=True,
                className="inv-search-input",
            ),
            html.Span(
                f"{shown} of {total} member sweeps",
                id="inv-search-note",
                className="annotate",
            ),
        ),
        html.Div(
            page.scroll_table(
                [
                    page.head_cell("Sweep"),
                    page.head_cell("Status"),
                    page.head_cell("Trials", numeric=True),
                    page.head_cell("Last activity", numeric=True),
                ],
                rows,
                sortable=True,
            ),
            id="inv-search-results",
        ),
    ]


def _via_investigation(
    service: DashboardService, project: str, via: str | None
) -> InvestigationRecord | None:
    """The investigation a ``via`` return path names, when it exists in
    this store and belongs to the same project; anything else is an
    unknown context and renders none."""
    if not via:
        return None
    try:
        record = service.investigation_detail(str(via)).investigation
    except (CurationRejectedError, CurationUnavailableError):
        return None
    return record if record.project == project else None


def sweep_hub_header(
    service: DashboardService,
    project: str,
    sweep_id: str,
    sweep_name: str,
    via: str | None,
) -> list[Any]:
    """Breadcrumb, back link, and the data-supported views row for a
    sweep opened from an investigation: Series and Points narrow to this
    member, Search opens over all members, Overview is the hub itself.
    A sweep reached outside an investigation renders no hub."""
    record = _via_investigation(service, project, via)
    if record is None:
        return []
    series_supported = any(
        entry["kind"] == "scalar" and entry["steps"]
        for entry in service.analysis_value_keys(project, {"sweeps": [sweep_id]})
    )
    detail = service.sweep_detail(sweep_id)
    points_supported = bool(detail and detail.overview.started)
    views: list[Any] = [html.Span("Overview", className="on")]
    if series_supported:
        views.append(
            html.A(
                "Series",
                href=analysis.investigation_view_href(
                    project, str(record.id), "series", sweep_id
                ),
            )
        )
    if points_supported:
        views.append(
            html.A(
                "Points",
                href=analysis.investigation_view_href(
                    project, str(record.id), "points", sweep_id
                ),
            )
        )
    views.append(
        html.A(
            "Search",
            href=analysis.investigation_view_href(project, str(record.id), "search"),
        )
    )
    return [
        page.breadcrumbs(
            [
                (project, f"{ROUTES_BASE}/project/{quote(project, safe='')}"),
                ("Investigations", investigations_index_href(project)),
                record.name,
                sweep_name,
            ]
        ),
        html.Div(
            [
                html.Span("Views", className="annotate"),
                html.Div(views, className="seg"),
                html.A(
                    f"Back to {record.name}",
                    href=analysis.investigation_view_href(
                        project, str(record.id), "compare"
                    ),
                    className="btn-link",
                ),
            ],
            className="inv-views",
        ),
    ]


def curation_transitions(archived: bool, invalid: bool) -> dict[str, bool]:
    """Which curation actions are valid transitions for one sweep."""
    return {
        "archive": not archived,
        "invalid": not invalid,
        "restore_validity": invalid,
        "restore": archived and not invalid,
    }
