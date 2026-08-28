"""Top-level dashboard shell and per-page operational views.

The shell owns all client-side state: ``dcc.Location`` carries the URL,
``project-store`` the active project, ``selection-store`` the unified
selection tray (sweeps picked on any grid, retry families, and the
expansion toggle — the typed ``Selection`` is built per query call in
``view-store`` the analysis view state the ``view=``
parameter round-trips, ``workspace-store`` the per-project workspace
review state (view, quick filter, column filters, sort),
``analysis-message-store`` the analysis
URL-hydration message, and ``poll`` is the conditional refresh interval
pages enable or disable through the router callback.

Every page function is pure: data in, Dash components out. Callbacks
fetch through DashboardService and hand the results here.
"""

import json

from dash import dcc, html
from dash_ag_grid import AgGrid
from jernerics_schema import ExecutionRecord

from . import artifacts, components
from .analysis import EMPTY_TRAY, default_view_state, series_entry_href
from .components import MISSING, UNKNOWN, Badge, short_id, time_cell
from .routes import ROUTES_BASE
from .service import (
    ExecutionDetail,
    FamilyRow,
    ProjectSummary,
    SweepDetail,
    SweepSummary,
    TrialDetail,
    view_counts,
)

POLL_INTERVAL_MS = 5000

_KIND_LABELS = {
    "workspace": "Project",
    "sweep": "Sweep",
    "trial": "Trial",
    "execution": "Execution",
    "artifact": "Artifact",
}

_MONITORING_ORDER = (
    "active",
    "quiet",
    "stale",
    "failed",
    "succeeded",
    UNKNOWN,
)


def shell() -> html.Div:
    """Top-level layout: URL state, nav bar, stores, outlet, poller."""
    return html.Div(
        [
            html.Link(
                rel="icon",
                type="image/svg+xml",
                href=f"{ROUTES_BASE}/assets/favicon.svg",
            ),
            dcc.Location(id="url", refresh=False),
            html.Header(
                className="nav",
                children=[
                    html.A("jernerics", href=f"{ROUTES_BASE}/", className="brand"),
                    html.A(
                        "Analysis",
                        href=f"{ROUTES_BASE}/analysis",
                        className="nav-link",
                    ),
                    dcc.Dropdown(
                        id="project-picker",
                        placeholder="Project…",
                        clearable=True,
                        className="project-picker",
                    ),
                    html.A(
                        id="selection-tray",
                        className="tray",
                        href=f"{ROUTES_BASE}/analysis",
                    ),
                    html.Form(
                        [
                            html.Button("Log out", type="submit", className="logout"),
                        ],
                        action=f"{ROUTES_BASE}/logout",
                        method="post",
                    ),
                ],
            ),
            html.Main(id="page-container", children=[project_page([], 0)]),
            dcc.Store(id="project-store", storage_type="session"),
            dcc.Store(
                id="selection-store", storage_type="session", data=dict(EMPTY_TRAY)
            ),
            dcc.Store(id="analysis-message-store"),
            dcc.Store(id="view-store", data=default_view_state()),
            dcc.Store(id="workspace-store", storage_type="session"),
            dcc.Interval(id="poll", interval=POLL_INTERVAL_MS, disabled=True),
        ],
        className="shell",
    )


def _sweep_href(sweep_id: str) -> str:
    return f"{ROUTES_BASE}/sweep/{sweep_id}"


def _trial_href(trial_id: str) -> str:
    return f"{ROUTES_BASE}/trial/{trial_id}"


def _execution_href(execution_id: str) -> str:
    return f"{ROUTES_BASE}/execution/{execution_id}"


def _project_href(project: str) -> str:
    return f"{ROUTES_BASE}/project/{project}"


def _grid_link(label: str, href: str) -> str:
    """Markdown cell value; AG Grid's markdown renderer turns it into an
    anchor, so the cell doubles as a doorway to the detail page."""
    safe_label = label.replace("[", "\\[").replace("]", "\\]")
    return f"[{safe_label}]({href})"


def _objective(objective: float | None) -> str:
    return MISSING if objective is None else f"{objective:g}"


def project_page(catalog: list[ProjectSummary], now_ns: int) -> html.Div:
    """Project catalog: health counts, recent sweep, last activity."""
    if not catalog:
        return html.Div(
            [
                html.H2("Projects"),
                components.Empty(
                    "No projects yet — tracking data appears here once a "
                    "sweep is ingested."
                ),
            ],
            className="page",
        )
    rows = [
        html.A(
            [
                html.Span(summary.project, className="project-name"),
                html.Span(
                    [
                        Badge(f"active {summary.active}", kind="active"),
                        Badge(f"stale {summary.stale}", kind="stale"),
                        Badge(f"failed {summary.failed}", kind="failed"),
                        *(
                            [
                                Badge(
                                    f"archived {summary.archived_sweeps}",
                                    kind="archived",
                                )
                            ]
                            if summary.archived_sweeps
                            else []
                        ),
                        *(
                            [Badge(f"invalid {summary.invalid_sweeps}", kind="invalid")]
                            if summary.invalid_sweeps
                            else []
                        ),
                    ],
                    className="project-counts",
                ),
                html.Span(summary.recent_sweep or MISSING, className="project-sweep"),
                html.Span(
                    components.relative_time(summary.last_activity_ns, now_ns),
                    className="project-activity",
                ),
            ],
            href=f"{ROUTES_BASE}/project/{summary.project}",
            className="project-row",
        )
        for summary in catalog
    ]
    return html.Div(
        [
            html.H2("Projects"),
            html.Div(
                [
                    html.Div(
                        [
                            html.Span("Project", className="project-name"),
                            html.Span("Execution health", className="project-counts"),
                            html.Span("Recent sweep", className="project-sweep"),
                            html.Span("Last activity", className="project-activity"),
                        ],
                        className="project-row project-head",
                    ),
                    *rows,
                ],
                className="project-list",
            ),
        ],
        className="page",
    )


def sweep_curation(summary: SweepSummary) -> str:
    """Distinct curation marker for grid cells; invalid outranks archived."""
    if summary.invalid:
        return "invalid"
    if summary.archived:
        return "archived"
    return ""


def sweep_grid_row(summary: SweepSummary, now_ns: int) -> dict[str, object]:
    """One AG Grid row dict for the workspace sweep grid."""
    return {
        "sweep_id": summary.sweep_id,
        "name": _grid_link(summary.name, _sweep_href(summary.sweep_id)),
        "state": summary.state,
        "submitted_jobs": summary.submitted_jobs,
        "expected_trials": (
            MISSING if summary.expected_trials is None else summary.expected_trials
        ),
        "started": summary.started,
        "terminal": summary.terminal,
        "optimizer": MISSING,
        "direction": MISSING,
        "backend": summary.backend or MISSING,
        "latest_submission": (
            MISSING
            if summary.latest_submitted_ns is None
            else components.relative_time(summary.latest_submitted_ns, now_ns)
        ),
        "health": summary.health,
        "curation": sweep_curation(summary),
        "archived": summary.archived,
        "invalid": summary.invalid,
    }


def sweep_grid_columns(sort: list | None) -> list[dict]:
    """Workspace column defs with a stored sort applied as each column's
    initial sort (AG Grid's documented restore point for colId/sort)."""
    by_field = {entry["colId"]: entry for entry in sort or []}
    columns = []
    for column in _SWEEP_GRID_COLUMNS:
        entry = by_field.get(column["field"])
        columns.append({**column, "sort": entry["sort"]} if entry else dict(column))
    return columns


_SWEEP_GRID_COLUMNS = [
    {
        "headerName": "Sweep",
        "field": "name",
        "cellRenderer": "markdown",
    },
    {"headerName": "State", "field": "state"},
    {"headerName": "Curation", "field": "curation"},
    {"headerName": "Submitted jobs", "field": "submitted_jobs"},
    {"headerName": "Expected trials", "field": "expected_trials"},
    {"headerName": "Started", "field": "started"},
    {"headerName": "Terminal", "field": "terminal"},
    {"headerName": "Optimizer", "field": "optimizer"},
    {"headerName": "Direction", "field": "direction"},
    {"headerName": "Backend", "field": "backend"},
    {"headerName": "Latest submission", "field": "latest_submission"},
    {"headerName": "Health", "field": "health"},
]


def view_options(counts: dict[str, int]) -> list[dict[str, str]]:
    """RadioItems options for the workspace view controls, with counts."""
    return [
        {"label": f" Current · {counts['current']}", "value": "current"},
        {"label": f" Archived · {counts['archived']}", "value": "archived"},
        {"label": f" All · {counts['all']}", "value": "all"},
    ]


def curation_transitions(archived: bool, invalid: bool) -> dict[str, bool]:
    """Which curation actions are valid transitions for one sweep."""
    return {
        "archive": not archived,
        "invalid": not invalid,
        "restore_validity": invalid,
        "restore": archived and not invalid,
    }


def selection_transitions(rows: list[dict] | None) -> dict[str, bool]:
    """Valid transitions for a grid selection: an action is offered when
    at least one selected row admits it."""
    per_row = [
        curation_transitions(bool(row.get("archived")), bool(row.get("invalid")))
        for row in rows or []
    ]
    return {
        action: any(transition[action] for transition in per_row)
        for action in curation_transitions(False, False)
    }


def curation_note(visible: list[SweepSummary]) -> str:
    """The active-work note when incomplete sweeps carry curation."""
    if any(
        summary.incomplete and (summary.archived or summary.invalid)
        for summary in visible
    ):
        return (
            "Curation does not cancel or hide active work — incomplete "
            "sweeps stay visible in Current while they run."
        )
    return ""


def action_message(ok: bool, text: str) -> html.Div:
    """Visible success/failure report for a curation action."""
    return html.Div(text, className=f"action-message {'ok' if ok else 'err'}")


def workspace_actions() -> html.Div:
    """Selected-row action bar; buttons enable per the selection."""
    return html.Div(
        [
            html.A(
                "Analyze",
                id="ws-analyze",
                className="action analyze-link",
                href=f"{ROUTES_BASE}/analysis",
            ),
            html.Button("Archive", id="ws-archive", disabled=True, className="action"),
            html.Button(
                "Mark invalid", id="ws-invalid", disabled=True, className="action"
            ),
            html.Button(
                "Restore validity",
                id="ws-restore-validity",
                disabled=True,
                className="action",
            ),
            html.Button("Restore", id="ws-restore", disabled=True, className="action"),
            dcc.Input(
                id="ws-reason",
                type="text",
                placeholder="Reason (required for Mark invalid)",
                className="reason-input",
            ),
        ],
        className="action-bar",
    )


def workspace_page(
    project: str,
    summaries: list[SweepSummary],
    selected_sweeps: list[str],
    now_ns: int,
    *,
    visible: list[SweepSummary] | None = None,
    counts: dict[str, int] | None = None,
    state: dict | None = None,
) -> html.Div:
    """Project workspace: view controls, quick search, the sweep grid
    that feeds the selection tray, and the curation action bar."""
    if not summaries:
        return html.Div(
            [
                html.H2(f"Project {project}"),
                components.Empty(f"No sweeps tracked for project {project} yet."),
            ],
            className="page",
        )
    state = state or {}
    view = state.get("view") or "current"
    quick = state.get("quick") or ""
    shown = summaries if visible is None else visible
    row_counts = view_counts(summaries) if counts is None else counts
    rows = [sweep_grid_row(summary, now_ns) for summary in shown]
    selected = [row for row in rows if row["sweep_id"] in set(selected_sweeps)]
    return html.Div(
        [
            html.H2(f"Project {project}"),
            html.Div(
                [
                    dcc.RadioItems(
                        id="workspace-view",
                        options=view_options(row_counts),
                        value=view,
                        inline=True,
                        className="workspace-views",
                    ),
                    dcc.Input(
                        id="workspace-quick",
                        value=quick,
                        type="search",
                        placeholder="Search sweeps…",
                        className="quick-filter",
                    ),
                ],
                className="workspace-controls",
            ),
            html.Div(
                curation_note(shown), id="workspace-curation-note", className="hint"
            ),
            html.P(
                "Optimizer and objective direction are owned by the "
                "optimizer journal, not the v3 store — they show as "
                f"{MISSING} until that provenance is ingested.",
                className="hint",
            ),
            AgGrid(
                id="sweep-grid",
                rowData=rows,
                columnDefs=sweep_grid_columns(state.get("sort")),
                defaultColDef={
                    "sortable": True,
                    "filter": True,
                    "resizable": True,
                    "minWidth": 100,
                },
                dashGridOptions=components.grid_options(
                    rowSelection={"mode": "multiRow"}, quickFilterText=quick
                ),
                filterModel=state.get("filters"),
                selectedRows=selected,
                className="ag-theme-alpine grid",
            ),
            workspace_actions(),
            html.Div(id="workspace-message"),
            html.P(
                "Select sweeps to load them into the shared selection tray.",
                className="hint",
            ),
        ],
        className="page",
    )


def _correlation_table(jobs: list[dict]) -> html.Table:
    rows = [
        [
            short_id(job["submission_id"]),
            job["backend"],
            Badge(job["submission_state"]),
            job["scheduler_job_id"] or MISSING,
            job["role"] or MISSING,
            Badge(job["job_state"]) if job["job_state"] else MISSING,
        ]
        for job in jobs
    ]
    return components.DataTable(
        (
            "Submission",
            "Backend",
            "Submission state",
            "Scheduler job",
            "Role",
            "Job state",
        ),
        rows,
    )


def _monitoring_counts(summary: SweepSummary) -> html.Div:
    return html.Div(
        [
            Badge(f"{label} {getattr(summary, label)}", kind=label)
            for label in _MONITORING_ORDER
        ],
        className="monitoring-row",
    )


def _progress_list(progress: list[dict]) -> html.Div:
    if not progress:
        return html.Div(html.P("No in-flight executions report progress."))
    return html.Div(
        html.Ul(
            [
                html.Li(
                    html.A(
                        f"{short_id(row['execution_id'])} · "
                        f"{row['current']}/{row['total']} {row['unit']}",
                        href=_execution_href(row["execution_id"]),
                    )
                )
                for row in progress
            ],
            className="progress-list",
        )
    )


def _executions_table(executions: list[ExecutionRecord], now_ns: int) -> html.Table:
    """One row per execution: monitoring badge, deep link, host, times."""
    return components.DataTable(
        ("Monitoring", "Execution", "Host", "Started", "Ended"),
        [
            (
                Badge(record.monitoring or UNKNOWN),
                html.A(
                    short_id(str(record.execution_id)),
                    href=_execution_href(str(record.execution_id)),
                ),
                record.hostname,
                time_cell(components.datetime_to_ns(record.started_at), now_ns),
                (
                    UNKNOWN
                    if record.ended_at is None
                    else time_cell(components.datetime_to_ns(record.ended_at), now_ns)
                ),
            )
            for record in executions
        ],
    )


def family_grid_row(family: FamilyRow) -> dict[str, object]:
    """One AG Grid row dict for the trial-family grid."""
    shown = ", ".join(f"{key}={value}" for key, value in family.params[:3])
    hidden = len(family.params) - 3
    return {
        "root": family.root,
        "root_short": _grid_link(short_id(family.root), _trial_href(family.root)),
        "current_trial": family.current_trial,
        "current_short": _grid_link(
            short_id(family.current_trial), _trial_href(family.current_trial)
        ),
        "number": family.number,
        "state": family.state,
        "objective": _objective(family.objective),
        "params": f"{shown}, +{hidden}" if hidden > 0 else shown,
        "retries": family.retry_count,
        "generations": family.generations,
    }


_FAMILY_GRID_COLUMNS = [
    {"headerName": "Family root", "field": "root_short", "cellRenderer": "markdown"},
    {
        "headerName": "Current trial",
        "field": "current_short",
        "cellRenderer": "markdown",
    },
    {"headerName": "#", "field": "number"},
    {"headerName": "State", "field": "state"},
    {"headerName": "Objective", "field": "objective"},
    {"headerName": "Params", "field": "params"},
    {"headerName": "Retries", "field": "retries"},
]


def lineage_chain(root: str | None, lineage: list[dict]) -> list[object]:
    """Side-panel lineage for one family: ordered generations with
    parent -> root -> index facts, exactly as stored."""
    if not root:
        return [html.P("Pick a family row to inspect its retry lineage.")]
    entries = sorted(
        (entry for entry in lineage if entry["root"] == root),
        key=lambda entry: entry["index"],
    )
    if not entries:
        return [html.P("No lineage facts for this family.")]
    return [
        html.P(
            " → ".join(short_id(entry["trial_id"]) for entry in entries),
            className="lineage-chain",
        ),
        components.DataTable(
            ("Index", "Trial", "Parent", "Root"),
            [
                (
                    entry["index"],
                    short_id(entry["trial_id"]),
                    short_id(entry["parent"]) if entry["parent"] else MISSING,
                    short_id(entry["root"]),
                )
                for entry in entries
            ],
        ),
    ]


def curation_banners(overview: SweepSummary, now_ns: int) -> list[html.Div]:
    """Archived/invalid banners naming the state; invalid carries the
    reason and its timestamp."""
    banners = []
    if overview.archived:
        banners.append(
            html.Div(
                [
                    Badge("archived", kind="archived"),
                    " This sweep is archived — curation changes review "
                    "visibility only; tracked facts and running work are "
                    "untouched.",
                ],
                className="curation-banner",
            )
        )
    if overview.invalid:
        banners.append(
            html.Div(
                [
                    Badge("invalid", kind="invalid"),
                    " Marked scientifically invalid at "
                    f"{components.absolute_time(overview.invalid_ns)} — "
                    f"reason: {overview.invalid_reason}. The data stays "
                    "queryable but must not be treated as valid science.",
                ],
                className="curation-banner curation-banner-invalid",
            )
        )
    return banners


def curation_actions(overview: SweepSummary, prefix: str) -> html.Div:
    """Archive/invalid/restore buttons offering only valid transitions."""
    offered = curation_transitions(overview.archived, overview.invalid)
    return html.Div(
        [
            html.Button(
                "Archive",
                id=f"{prefix}-archive",
                disabled=not offered["archive"],
                className="action",
            ),
            html.Button(
                "Mark invalid",
                id=f"{prefix}-invalid",
                disabled=not offered["invalid"],
                className="action",
            ),
            html.Button(
                "Restore validity",
                id=f"{prefix}-restore-validity",
                disabled=not offered["restore_validity"],
                className="action",
            ),
            html.Button(
                "Restore",
                id=f"{prefix}-restore",
                disabled=not offered["restore"],
                className="action",
            ),
        ],
        className="action-bar",
    )


def detail_curation(overview: SweepSummary, now_ns: int) -> html.Div:
    """Sweep-detail banners plus the action row (refreshed after a
    mutation so button availability follows the new state)."""
    return html.Div(
        [
            *curation_banners(overview, now_ns),
            curation_actions(overview, "detail"),
        ]
    )


def sweep_page(detail: SweepDetail, now_ns: int) -> html.Div:
    """Sweep page: project breadcrumb, submission/job correlation,
    monitoring, every execution, in-flight progress, and the trial-family
    grid with its lineage side panel."""
    overview = detail.overview
    return html.Div(
        [
            html.H2(f"Sweep {overview.name} · {short_id(overview.sweep_id)}"),
            html.P(
                [
                    Badge(overview.state),
                    Badge(f"health {overview.health}", kind=overview.health),
                    html.A(
                        f"project {detail.context['project']}",
                        href=_project_href(detail.context["project"]),
                    ),
                    html.A(
                        "Analyze series",
                        href=series_entry_href(
                            detail.context["project"], overview.sweep_id
                        ),
                        className="analyze-link",
                    ),
                ]
            ),
            html.Div(
                [
                    html.Div(
                        detail_curation(overview, now_ns),
                        id="detail-curation",
                    ),
                    dcc.Input(
                        id="detail-reason",
                        type="text",
                        placeholder="Reason (required for Mark invalid)",
                        className="reason-input",
                    ),
                    html.Div(id="detail-message"),
                ],
                className="curation-section",
            ),
            html.Section(
                [
                    html.H3("Submissions & jobs"),
                    _correlation_table(detail.jobs),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Execution monitoring"),
                    _monitoring_counts(overview),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Executions"),
                    _executions_table(detail.executions, now_ns),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("In-flight progress"),
                    _progress_list(detail.progress),
                ],
                className="section",
            ),
            html.Section(
                [
                    html.H3("Trial families"),
                    html.Div(
                        [
                            AgGrid(
                                id="family-grid",
                                rowData=[
                                    family_grid_row(family)
                                    for family in detail.families
                                ],
                                columnDefs=_FAMILY_GRID_COLUMNS,
                                defaultColDef={
                                    "sortable": True,
                                    "filter": True,
                                    "resizable": True,
                                    "minWidth": 90,
                                },
                                dashGridOptions=components.grid_options(
                                    rowSelection={"mode": "singleRow"}
                                ),
                                className="ag-theme-alpine grid",
                            ),
                            html.Div(
                                [
                                    html.P(
                                        "Pick a family row to inspect its retry "
                                        "lineage."
                                    )
                                ],
                                id="family-lineage-panel",
                                className="lineage-panel",
                            ),
                        ],
                        className="family-layout",
                    ),
                ],
                className="section",
            ),
            dcc.Store(id="family-lineage-store", data={"lineage": detail.lineage}),
        ],
        className="page",
    )


def trial_page(detail: TrialDetail, now_ns: int) -> html.Div:
    """Trial page: family header, optimizer state, params, value catalog,
    and the family's executions."""
    context = detail.context
    chain = [
        entry
        for entry in detail.lineage
        if entry["root"] == context["retry_root_trial_id"]
    ]
    chain.sort(key=lambda entry: entry["index"])
    header_bits = [
        html.Span(
            " → ".join(short_id(entry["trial_id"]) for entry in chain)
            or short_id(context["trial_id"]),
            className="lineage-chain",
        ),
        html.Span(
            f"retry index {context['retry_index']} · root "
            f"{short_id(context['retry_root_trial_id'])}"
        ),
    ]
    params_table = components.DataTable(
        ("Kind", "Key", "Value"),
        [(record.kind, record.key, str(record.value)) for record in detail.params],
    )
    catalog_table = components.DataTable(
        ("Key", "Kind", "Points", "Latest step", "Trials"),
        [
            (
                record.key,
                record.kind,
                record.n_points,
                record.latest_step,
                record.n_trials,
            )
            for record in detail.catalog
        ],
    )
    executions_table = _executions_table(detail.executions, now_ns)
    return html.Div(
        [
            html.H2(f"Trial {short_id(context['trial_id'])}"),
            html.P(header_bits, className="trial-header"),
            html.Section(
                [
                    html.H3("Optimizer trial state"),
                    html.P(
                        [
                            Badge(context["state"]),
                            html.Span(f"objective {_objective(context['objective'])}"),
                            html.Span(f"number {context['number']}"),
                            html.A(
                                f"sweep {context['sweep_name']}",
                                href=_sweep_href(context["sweep_id"]),
                            ),
                        ],
                        className="fact-row",
                    ),
                ],
                className="section",
                id="section-optimizer-state",
            ),
            html.Section([html.H3("Params"), params_table], className="section"),
            html.Section(
                [html.H3("Value catalog"), catalog_table], className="section"
            ),
            html.Section(
                [html.H3("Executions"), executions_table], className="section"
            ),
            html.Section(
                [
                    html.H3("Artifacts"),
                    artifacts.artifact_grid(detail.artifacts, now_ns),
                ],
                className="section",
            ),
        ],
        className="page",
    )


def execution_page(detail: ExecutionDetail, now_ns: int) -> html.Div:
    """Execution page: factual timeline/outcome/progress/config/provenance
    first, optimizer trial state in its own clearly separated section."""
    context = detail.context
    timeline = components.DataTable(
        ("Fact", "When"),
        [
            ("Started", time_cell(context["started_ns"], now_ns)),
            (
                "Last heartbeat",
                (
                    UNKNOWN
                    if context["last_heartbeat_ns"] is None
                    else time_cell(context["last_heartbeat_ns"], now_ns)
                ),
            ),
            (
                "Last observation",
                (
                    UNKNOWN
                    if context["last_observation_ns"] is None
                    else time_cell(context["last_observation_ns"], now_ns)
                ),
            ),
            (
                "Ended",
                (
                    UNKNOWN
                    if context["ended_ns"] is None
                    else time_cell(context["ended_ns"], now_ns)
                ),
            ),
        ],
    )
    progress = context["progress"]
    outcome_bits = [
        Badge(context["monitoring"] or UNKNOWN),
        html.Span(f"outcome {context['outcome'] or UNKNOWN}"),
        html.Span(
            f"exit {UNKNOWN if context['exit_code'] is None else context['exit_code']}"
        ),
    ]
    if context["failure_summary"]:
        outcome_bits.append(
            html.Span(
                f"{context['failure_kind'] or UNKNOWN}: {context['failure_summary']}",
                className="failure-summary",
            )
        )
    params_table = components.DataTable(
        ("Kind", "Key", "Value"),
        [(record.kind, record.key, str(record.value)) for record in detail.params],
    )
    provenance_table = components.DataTable(
        ("Submission", "Backend", "Submitted", "Expected", "Git", "Config"),
        [
            (
                short_id(str(record.submission_id)),
                record.backend,
                (
                    MISSING
                    if record.submitted_at_ns is None
                    else components.relative_time(record.submitted_at_ns, now_ns)
                ),
                (MISSING if record.expected_trials is None else record.expected_trials),
                record.git_hash or MISSING,
                record.config_source or MISSING,
            )
            for record in detail.provenance
        ],
    )
    facts = html.Section(
        [
            html.H3("Execution facts"),
            html.P(
                [
                    html.Span(f"host {context['hostname']}"),
                    *outcome_bits,
                    html.Span(
                        "progress "
                        + (
                            f"{progress['current']}/{progress['total']} "
                            f"{progress['unit']}"
                            if progress
                            else UNKNOWN
                        )
                    ),
                ],
                className="fact-row",
            ),
            html.H4("Timeline"),
            timeline,
            html.H4("Params"),
            params_table,
            html.H4("Resolved config"),
            html.Pre(
                json.dumps(detail.resolved_config, indent=2, sort_keys=True)
                if detail.resolved_config is not None
                else UNKNOWN,
                className="config-json",
            ),
            html.H4("Provenance"),
            provenance_table,
        ],
        className="section",
        id="section-execution-facts",
    )
    optimizer = html.Section(
        [
            html.H3("Optimizer trial state"),
            html.P(
                [
                    Badge(context["trial_state"]),
                    html.Span(f"objective {_objective(context['objective'])}"),
                    html.Span(f"number {context['number']}"),
                    html.Span(
                        f"retry index {context['retry_index']} · root "
                        f"{short_id(context['retry_root_trial_id'])}"
                    ),
                ],
                className="fact-row",
            ),
            html.P(
                [
                    html.A(
                        f"trial {short_id(context['trial_id'])}",
                        href=_trial_href(context["trial_id"]),
                    ),
                    html.A(
                        f"sweep {context['sweep_name']}",
                        href=_sweep_href(context["sweep_id"]),
                    ),
                ],
                className="fact-row",
            ),
        ],
        className="section",
        id="section-optimizer-state",
    )
    artifact_section = html.Section(
        [
            html.H3("Artifacts"),
            artifacts.artifact_grid(detail.artifacts, now_ns),
        ],
        className="section",
        id="section-execution-artifacts",
    )
    return html.Div(
        [
            html.H2(f"Execution {short_id(context['execution_id'])}"),
            facts,
            artifact_section,
            optimizer,
        ],
        className="page",
    )


def missing_object_page(kind: str, object_id: str) -> html.Div:
    """Deep link to an id the store does not know (or a malformed id)."""
    label = _KIND_LABELS.get(kind, kind)
    return html.Div(
        [
            html.H2(f"{label} {object_id}"),
            components.Empty(f"No {label.lower()} matches {object_id} in this store."),
        ],
        className="page",
    )


def not_found_page(pathname: str) -> html.Div:
    """Unknown dashboard path (client-side 404 surface)."""
    return html.Div(
        [
            html.H2("Not found"),
            components.Error(f"No dashboard route matches {pathname}."),
        ],
        className="page",
    )
