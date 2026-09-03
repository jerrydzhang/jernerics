import json
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any
from uuid import UUID

from dash import dcc, html
from dash_ag_grid import AgGrid
from jernerics_schema import (
    ExecutionRecord,
    InvestigationRecord,
    Selection,
    encode_selection,
    materialize_selection,
)

from . import analysis, artifacts, components, figures
from .components import MISSING, UNKNOWN, Badge, short_id, time_cell
from .render import SortColumn, sort_rows, sortable_columns
from .routes import ROUTES_BASE
from .service import (
    CompareDocument,
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
    ExecutionDetail,
    FailedExecutionRow,
    FamilyRow,
    InvestigationPreview,
    InvestigationRow,
    SweepDetail,
    SweepSummary,
    TrialDetail,
)

FOCUS_KINDS = ("sweep", "trial", "execution")

INVESTIGATION_VIEWS = analysis.INVESTIGATION_VIEWS
"""Re-exported view vocabulary; the ``view=`` codec owns the names."""

_INCOMPLETE_TRIAL_STATES = ("waiting", "running")


_FAILED_VIEW_LIMIT = 200
_OVERVIEW_PAGE_SIZE = 25
_MONITORING_ORDER = ("active", "quiet", "stale", "failed", "succeeded", UNKNOWN)
_STATE_TILE_LABELS = {
    "completed": "completed sweeps",
    "failed": "failed sweeps",
    "no-data": "sweeps with no trials yet",
    "running": "running sweeps",
}
_FILTER_CHIP_LABELS = {
    "failed": "with failed executions",
    "stale": "interrupted",
}

_GRID_DEFAULTS: dict[str, Any] = {
    "sortable": True,
    "resizable": True,
    "minWidth": 100,
}

_SWEEP_ROW_ID: Any = "params.data.sweep_id"
_TRIAL_ROW_ID: Any = "params.data.root || params.data.trial_id"


def focus_ref(kind: str, object_id: str) -> str:
    """Pattern-id token naming one focusable object."""
    return f"{kind}:{object_id}"


def focus_button(label: str, kind: str, object_id: str) -> html.Button:
    """In-page focus control; never navigates, never touches scope."""
    return html.Button(
        label, id={"focus-object": focus_ref(kind, object_id)}, className="focus-link"
    )


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


def browser_sweep_rows(
    summaries: Sequence[SweepSummary],
    tray: dict[str, Any] | None,
    *,
    include_archived: bool = False,
    include_invalid: bool = False,
    now_ns: int | None = None,
) -> list[dict[str, Any]]:
    """Browser sweep rows; checkbox state mirrors the tray's sweeps.

    Terminal archived/invalid sweeps stay out of discovery until the include
    controls reveal them; sweeps already picked are never dropped, and
    incomplete sweeps always stay discoverable.
    """
    now = time.time_ns() if now_ns is None else now_ns
    picked = set((tray or {}).get("sweeps") or [])
    rows = []
    for summary in summaries:
        if (
            hidden_curation(
                summary,
                include_archived=include_archived,
                include_invalid=include_invalid,
            )
            and not summary.incomplete
            and summary.sweep_id not in picked
        ):
            continue
        rows.append(
            {
                "sweep_id": summary.sweep_id,
                "name": summary.name,
                "state": summary.state,
                "curation": sweep_curation(summary),
                "archived": summary.archived,
                "invalid": summary.invalid,
                "incomplete": summary.incomplete,
                "submitted_jobs": summary.submitted_jobs,
                "expected_trials": (
                    MISSING
                    if summary.expected_trials is None
                    else summary.expected_trials
                ),
                "backend": summary.backend or MISSING,
                "latest_submission": (
                    MISSING
                    if summary.latest_submitted_ns is None
                    else components.relative_time(summary.latest_submitted_ns, now)
                ),
                "health": summary.health,
            }
        )
    return rows


_BROWSER_SWEEP_COLUMNS: list[dict[str, Any]] = [
    {"headerName": "Sweep", "field": "name"},
    {"headerName": "State", "field": "state"},
    {"headerName": "Curation", "field": "curation"},
    {"headerName": "Submitted jobs", "field": "submitted_jobs"},
    {"headerName": "Expected trials", "field": "expected_trials"},
    {"headerName": "Backend", "field": "backend"},
    {"headerName": "Latest submission", "field": "latest_submission"},
    {"headerName": "Health", "field": "health"},
]


def browser_sweep_columns(sort: list | None) -> list[dict[str, Any]]:
    """Browser column defs with a stored sort applied as each column's
    initial sort (AG Grid's documented restore point for colId/sort)."""
    by_field = {entry["colId"]: entry for entry in sort or []}
    columns = []
    for column in _BROWSER_SWEEP_COLUMNS:
        entry = by_field.get(column["field"])
        columns.append({**column, "sort": entry["sort"]} if entry else dict(column))
    return columns


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


def curation_note(rows: list[dict[str, Any]] | None) -> str:
    """Why curated sweeps still appear: incomplete ones stay visible
    while active, named by sweep and state so the marker cannot read
    as a no-op."""
    curated = [row for row in rows or [] if row.get("curation")]
    if not curated:
        return ""
    active = [row for row in curated if row.get("incomplete")]
    if active:
        named = ", ".join(f"{row['name']} is {row['curation']}" for row in active)
        return (
            f"{named} but still active — curation does not cancel or hide "
            "active work: incomplete sweeps stay visible and selectable "
            "while they run."
        )
    return (
        "Curated sweeps are listed only because they are picked or "
        "included — curation changes review visibility only; tracked "
        "facts are untouched."
    )


def action_message(ok: bool, text: str) -> html.Div:
    """Visible success/failure report for a curation action."""
    return html.Div(text, className=f"action-message {'ok' if ok else 'err'}")


def workspace_actions() -> html.Div:
    """Selected-row action bar; buttons enable per the scope selection."""
    return html.Div(
        [
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
                style={"display": "none"},
            ),
        ],
        className="action-bar",
    )


def curation_summary(picked: int) -> str:
    """Summary label naming how many rows the curation panel acts on."""
    return f"Curation ({picked} picked)" if picked else "Curation…"


def curation_panel() -> html.Details:
    """Collapsed affordance around the bulk curation actions; the
    active-work note stays outside it."""
    return html.Details(
        [
            html.Summary("Curation…", id="ws-curation-summary"),
            workspace_actions(),
            html.Div(id="workspace-message"),
        ],
        id="curation-panel",
        className="curation-panel",
    )


def scope_bar(
    service: DashboardService | None,
    project: str | None,
    tray: dict[str, Any] | None,
) -> html.Div:
    """The persistent scope line: ``All sweeps`` or the picked sweep names,
    curation badges, and the tray's counts."""
    tray = tray or analysis.EMPTY_TRAY
    if not project or service is None:
        return html.Div(
            components.Empty("Pick a project in the header to browse its sweeps."),
            className="scope-bar",
        )
    summaries = {
        summary.sweep_id: summary for summary in service.sweep_overview(project)
    }
    picked_ids = list(tray.get("sweeps") or [])
    picked = [
        (summaries[sweep_id].name if sweep_id in summaries else short_id(sweep_id))
        for sweep_id in picked_ids
    ]
    label = ", ".join(picked) if picked else "All sweeps"
    children: list[Any] = [
        html.Span(f"Scope: {label}", className="scope-sweeps"),
        html.Span(analysis.tray_summary(tray), className="scope-counts"),
    ]
    invalid: list[tuple[str, str]] = []
    for sweep_id, name in zip(picked_ids, picked, strict=True):
        summary = summaries.get(sweep_id)
        if summary is None:
            continue
        if summary.archived:
            children.append(Badge(f"{name} archived", kind="archived"))
        if summary.invalid:
            children.append(Badge(f"{name} invalid", kind="invalid"))
            invalid.append((name, summary.invalid_reason or "unrecorded"))
    if len(invalid) == 1:
        name, reason = invalid[0]
        children.append(
            html.Span(
                f"{name} is marked scientifically invalid — reason: "
                f"{reason}. Continue only with that in mind, or remove it "
                "from the scope.",
                className="scope-warning",
            )
        )
    elif invalid:
        children.append(
            html.Details(
                [
                    html.Summary(
                        f"{len(invalid)} of {len(picked)} picked sweeps marked invalid",
                        className="scope-warning-summary",
                    ),
                    html.Ul(
                        [html.Li(f"{name}: {reason}") for name, reason in invalid],
                        className="scope-warning-list",
                    ),
                ],
                className="scope-warning-details",
            )
        )
    return html.Div(children, className="scope-bar")


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


def _monitoring_badges(counts: dict[str, int]) -> list[html.Span]:
    """One pill per nonzero monitoring label, in the canonical order; an
    all-zero scope renders a single quiet note."""
    badges = [
        Badge(f"{label} {counts[label]}", kind=label)
        for label in _MONITORING_ORDER
        if counts.get(label)
    ]
    return badges or [html.Span("quiet", className="quiet-note")]


def _monitoring_counts(summary: SweepSummary) -> html.Div:
    return html.Div(
        _monitoring_badges(
            {label: getattr(summary, label) for label in _MONITORING_ORDER}
        ),
        className="monitoring-row",
    )


def _progress_list(progress: list[dict]) -> html.Div:
    if not progress:
        return html.Div()
    return html.Div(
        html.Ul(
            [
                html.Li(
                    focus_button(
                        f"{short_id(row['execution_id'])} · "
                        f"{row['current']}/{row['total']} {row['unit']}",
                        "execution",
                        row["execution_id"],
                    )
                )
                for row in progress
            ],
            className="progress-list",
        )
    )


def _executions_table(executions: Sequence[ExecutionRecord], now_ns: int) -> html.Table:
    """One row per execution: monitoring badge, focus control, short
    host, and single-line timestamps (absolute time in the tooltip)."""
    return components.DataTable(
        ("Monitoring", "Execution", "Host", "Started", "Ended"),
        [
            (
                Badge(record.monitoring or UNKNOWN),
                focus_button(
                    short_id(str(record.execution_id)),
                    "execution",
                    str(record.execution_id),
                ),
                components.short_host(record.hostname),
                components.time_cell_compact(
                    components.datetime_to_ns(record.started_at), now_ns
                ),
                (
                    UNKNOWN
                    if record.ended_at is None
                    else components.time_cell_compact(
                        components.datetime_to_ns(record.ended_at), now_ns
                    )
                ),
            )
            for record in executions
        ],
    )


_FAMILY_GRID_COLUMNS: list[dict[str, Any]] = [
    {"headerName": "Family root", "field": "root_short"},
    {"headerName": "Current trial", "field": "current_short"},
    {"headerName": "#", "field": "number"},
    {"headerName": "State", "field": "state"},
    {"headerName": "Objective", "field": "objective"},
    {"headerName": "Params", "field": "params"},
    {"headerName": "Retries", "field": "retries"},
]


def family_grid_row(family: FamilyRow) -> dict[str, object]:
    """One AG Grid row dict for the trial-family grid."""
    shown = ", ".join(f"{key}={value}" for key, value in family.params[:3])
    hidden = len(family.params) - 3
    return {
        "root": family.root,
        "root_short": short_id(family.root),
        "current_trial": family.current_trial,
        "current_short": short_id(family.current_trial),
        "number": family.number,
        "state": family.state,
        "objective": components.objective_text(family.objective),
        "params": f"{shown}, +{hidden}" if hidden > 0 else shown,
        "retries": family.retry_count,
        "generations": family.generations,
    }


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


def curation_banners(overview: SweepSummary) -> list[html.Div]:
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


def detail_curation(overview: SweepSummary) -> html.Div:
    """Sweep-inspector banners plus the action row (refreshed after a
    mutation so button availability follows the new state)."""
    offered = curation_transitions(overview.archived, overview.invalid)
    actions = html.Div(
        [
            html.Button(
                "Archive",
                id="detail-archive",
                disabled=not offered["archive"],
                className="action",
            ),
            html.Button(
                "Mark this sweep invalid",
                id="detail-invalid",
                disabled=not offered["invalid"],
                className="action",
            ),
            html.Button(
                "Restore validity",
                id="detail-restore-validity",
                disabled=not offered["restore_validity"],
                className="action",
            ),
            html.Button(
                "Restore",
                id="detail-restore",
                disabled=not offered["restore"],
                className="action",
            ),
        ],
        className="action-bar",
    )
    return html.Div([*curation_banners(overview), actions])


_PROGRESS_SHOWN = 10

_LINEAGE_STORE_CAP = 1000

_EXECUTION_ROW_ID: Any = "params.data.execution_id"

_EXECUTION_GRID_COLUMNS: list[dict[str, Any]] = [
    {
        "headerName": "Monitoring",
        "field": "monitoring",
        "cellClass": {"function": "'cell-state state-' + (params.value || 'unknown')"},
    },
    {"headerName": "Execution", "field": "execution_short"},
    {"headerName": "Host", "field": "host"},
    {"headerName": "Started", "field": "started", "tooltipField": "started_at"},
    {"headerName": "Ended", "field": "ended", "tooltipField": "ended_at"},
]


def _execution_grid_rows(
    executions: Sequence[ExecutionRecord], now_ns: int
) -> list[dict[str, Any]]:
    """One virtualized grid row per execution: monitoring label, focus
    target, host, and relative recency with absolute tooltips."""
    started_ns = [components.datetime_to_ns(record.started_at) for record in executions]
    ended_ns = [
        None if record.ended_at is None else components.datetime_to_ns(record.ended_at)
        for record in executions
    ]
    return [
        {
            "execution_id": str(record.execution_id),
            "monitoring": record.monitoring or UNKNOWN,
            "execution_short": short_id(str(record.execution_id)),
            "host": components.short_host(record.hostname),
            "started": components.relative_time(row_started, now_ns),
            "started_at": components.absolute_time(row_started),
            "ended": (
                UNKNOWN
                if row_ended is None
                else components.relative_time(row_ended, now_ns)
            ),
            "ended_at": (
                UNKNOWN if row_ended is None else components.absolute_time(row_ended)
            ),
        }
        for record, row_started, row_ended in zip(
            executions, started_ns, ended_ns, strict=True
        )
    ]


def _lineage_store_rows(lineage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest whole retry families up to the store cap; trimmed roots
    report no lineage facts in the side panel."""
    if len(lineage) <= _LINEAGE_STORE_CAP:
        return lineage
    trimmed = lineage[-_LINEAGE_STORE_CAP:]
    head = trimmed[0]["root"]
    for index, entry in enumerate(trimmed):
        if entry["root"] != head:
            return trimmed[index:]
    return []


def _sweep_sections(detail: SweepDetail, now_ns: int) -> list[Any]:
    overview = detail.overview
    return [
        html.Div(
            [
                html.Div(detail_curation(overview), id="detail-curation"),
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
            [html.H3("Submissions & jobs"), _correlation_table(detail.jobs)],
            className="section",
        ),
        html.Section(
            [html.H3("Execution monitoring"), _monitoring_counts(overview)],
            className="section",
        ),
        html.Section(
            [
                html.H3("Executions"),
                AgGrid(
                    id={"focus-executions": "grid"},
                    rowData=_execution_grid_rows(detail.executions, now_ns),
                    columnDefs=_EXECUTION_GRID_COLUMNS,
                    defaultColDef={**_GRID_DEFAULTS, "minWidth": 90},
                    dashGridOptions=components.grid_options(),
                    getRowId=_EXECUTION_ROW_ID,
                    className="ag-theme-alpine grid",
                ),
            ],
            className="section",
        ),
        html.Section(
            [
                html.H3("In-flight progress"),
                _progress_list(detail.progress[:_PROGRESS_SHOWN]),
                *(
                    [
                        html.P(
                            f"…and {len(detail.progress) - _PROGRESS_SHOWN} more "
                            "in flight",
                            className="hint",
                        )
                    ]
                    if len(detail.progress) > _PROGRESS_SHOWN
                    else []
                ),
            ],
            className="section",
        ),
        html.Section(
            [
                html.H3("Trial families"),
                html.Div(
                    [
                        AgGrid(
                            id={"focus-family": "grid"},
                            rowData=[
                                family_grid_row(family) for family in detail.families
                            ],
                            columnDefs=_FAMILY_GRID_COLUMNS,
                            defaultColDef={**_GRID_DEFAULTS, "minWidth": 90},
                            dashGridOptions=components.grid_options(),
                            getRowId=_TRIAL_ROW_ID,
                            className="ag-theme-alpine grid",
                        ),
                        html.Div(
                            [html.P("Pick a family row to inspect its retry lineage.")],
                            id="family-lineage-panel",
                            className="lineage-panel",
                        ),
                    ],
                    className="family-layout",
                ),
            ],
            className="section",
        ),
        dcc.Store(
            id="family-lineage-store",
            data={"lineage": _lineage_store_rows(detail.lineage)},
        ),
    ]


def _trial_sections(detail: TrialDetail, now_ns: int) -> list[Any]:
    context = detail.context
    chain = sorted(
        (
            entry
            for entry in detail.lineage
            if entry["root"] == context["retry_root_trial_id"]
        ),
        key=lambda entry: entry["index"],
    )
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
    return [
        html.P(header_bits, className="trial-header"),
        html.Section(
            [
                html.H3("Optimizer trial state"),
                html.P(
                    [
                        Badge(context["state"]),
                        html.Span(
                            "objective "
                            + f"{components.objective_text(context['objective'])}"
                        ),
                        html.Span(f"number {context['number']}"),
                        focus_button(
                            f"sweep {context['sweep_name']}",
                            "sweep",
                            context["sweep_id"],
                        ),
                    ],
                    className="fact-row",
                ),
            ],
            className="section",
            id="section-optimizer-state",
        ),
        html.Section(
            [
                html.H3("Params"),
                components.DataTable(
                    ("Kind", "Key", "Value"),
                    [
                        (record.kind, record.key, str(record.value))
                        for record in detail.params
                    ],
                ),
            ],
            className="section",
        ),
        html.Section(
            [
                html.H3("Value catalog"),
                components.DataTable(
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
                ),
            ],
            className="section",
        ),
        html.Section(
            [html.H3("Executions"), _executions_table(detail.executions, now_ns)],
            className="section",
        ),
        html.Section(
            [
                html.H3("Artifacts"),
                artifacts.artifact_grid(detail.artifacts, now_ns),
            ],
            className="section",
        ),
    ]


def _execution_sections(detail: ExecutionDetail, now_ns: int) -> list[Any]:
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
            components.DataTable(
                ("Kind", "Key", "Value"),
                [
                    (record.kind, record.key, str(record.value))
                    for record in detail.params
                ],
            ),
            html.H4("Resolved config"),
            html.Pre(
                json.dumps(detail.resolved_config, indent=2, sort_keys=True)
                if detail.resolved_config is not None
                else UNKNOWN,
                className="config-json",
            ),
            html.H4("Provenance"),
            components.DataTable(
                ("Submission", "Backend", "Submitted", "Expected", "Git", "Config"),
                [
                    (
                        short_id(str(record.submission_id)),
                        record.backend,
                        (
                            MISSING
                            if record.submitted_at_ns is None
                            else components.relative_time(
                                record.submitted_at_ns, now_ns
                            )
                        ),
                        (
                            MISSING
                            if record.expected_trials is None
                            else record.expected_trials
                        ),
                        record.git_hash or MISSING,
                        record.config_source or MISSING,
                    )
                    for record in detail.provenance
                ],
            ),
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
                    html.Span(
                        f"objective {components.objective_text(context['objective'])}"
                    ),
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
                    focus_button(
                        f"trial {short_id(context['trial_id'])}",
                        "trial",
                        context["trial_id"],
                    ),
                    focus_button(
                        f"sweep {context['sweep_name']}", "sweep", context["sweep_id"]
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
    return [facts, artifact_section, optimizer]


def inspector_placeholder() -> html.Div:
    """No-focus inspector region; keeps the close control in the layout."""
    return html.Div(
        [
            html.Button(
                "✕",
                id="inspector-close",
                title="Close inspector",
                style={"display": "none"},
            ),
            "Click a sweep, trial, or execution row to inspect it here.",
        ],
        className="inspector-hint inspector-placeholder",
    )


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
        investigation_crumb(project, record.name, sweep_name),
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


def inspector_content(
    service: DashboardService,
    focus: dict[str, Any] | None,
    now_ns: int,
    project: str = "",
    via: str | None = None,
) -> html.Div:
    """The focused object's factual content; a missing id is named, not
    hidden. A sweep focus opened from an investigation carries the hub:
    the investigation breadcrumb, the back link, and the member-scoped
    views row."""
    if not focus:
        return inspector_placeholder()
    kind, object_id = focus.get("kind"), str(focus.get("id") or "")

    if kind == "sweep":
        detail = service.sweep_detail(object_id)
        body = (
            _sweep_sections(detail, now_ns)
            if detail is not None
            else [components.Empty(f"No sweep matches {object_id} in this store.")]
        )
        heading = (
            f"Sweep {detail.overview.name} · {short_id(detail.overview.sweep_id)}"
            if detail is not None
            else f"Sweep {object_id}"
        )
        hub = sweep_hub_header(
            service,
            project,
            object_id,
            detail.overview.name if detail else object_id,
            via,
        )
    elif kind == "trial":
        detail = service.trial_detail(object_id)
        body = (
            _trial_sections(detail, now_ns)
            if detail is not None
            else [components.Empty(f"No trial matches {object_id} in this store.")]
        )
        heading = f"Trial {short_id(object_id)}"
        hub = []
    elif kind == "execution":
        detail = service.execution_detail(object_id)
        body = (
            _execution_sections(detail, now_ns)
            if detail is not None
            else [components.Empty(f"No execution matches {object_id} in this store.")]
        )
        heading = f"Execution {short_id(object_id)}"
        hub = []
    else:
        return html.Div(
            components.Empty(f"Unknown focus kind {kind!r}."),
            className="inspector-body",
        )
    return html.Div(
        [
            html.Div(
                [
                    html.H3(heading, className="inspector-title"),
                    html.Button("✕", id="inspector-close", title="Close inspector"),
                ],
                className="inspector-header",
            ),
            *hub,
            html.Div(body, className="inspector-body"),
        ],
        className="inspector-panel",
    )


def _badge_cell(field: str) -> dict[str, Any]:
    """Column def rendering the field's value as a badge-styled cell."""
    return {
        "field": field,
        "cellClass": {"function": "'cell-state state-' + (params.value || 'unknown')"},
    }


_OVERVIEW_SWEEP_COLUMNS: list[SortColumn] = [
    SortColumn("name", "Sweep", "string"),
    SortColumn("state", "State", "string", definition=_badge_cell("state")),
    SortColumn("health", "Health", "string", definition=_badge_cell("health")),
    SortColumn(
        "monitoring",
        "Monitoring",
        "string",
        definition={**components.clamped_column(), "maxWidth": 320},
    ),
    SortColumn("curation", "Curation", "string"),
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


def _monitoring_summary(summary: SweepSummary) -> str:
    """Compact nonzero monitoring text for one overview grid row."""
    parts = [
        f"{label} {getattr(summary, label)}"
        for label in _MONITORING_ORDER
        if getattr(summary, label)
    ]
    return " · ".join(parts) if parts else MISSING


def overview_sweep_rows(scoped: Sequence[SweepSummary]) -> list[dict[str, Any]]:
    """One overview-grid row per scoped sweep; stamps and counts stay
    raw so the columns sort typed, and the grid formats at view time."""
    return [
        {
            "sweep_id": summary.sweep_id,
            "name": summary.name,
            "state": summary.state,
            "health": summary.health,
            "monitoring": _monitoring_summary(summary),
            "curation": sweep_curation(summary),
            "expected_trials": summary.expected_trials,
            "last_activity_ns": summary.latest_submitted_ns,
        }
        for summary in scoped
    ]


def counted_sweeps(count: int) -> str:
    """``count`` with the noun form that matches it."""
    return f"{count} sweep" if count == 1 else f"{count} sweeps"


def overview_tiles(scoped: Sequence[SweepSummary]) -> list[dict[str, Any]]:
    """Operational tiles for the scope: execution health first, then one
    per observed sweep state. Only nonzero facts render a tile, and
    every tile filters the sweep table — one uniform affordance."""
    tiles: list[dict[str, Any]] = []
    failing = [summary for summary in scoped if summary.failed]
    if failing:
        tiles.append(
            {
                "value": "failed",
                "kind": "crit",
                "count": sum(summary.failed for summary in failing),
                "label": f"failed executions · {counted_sweeps(len(failing))}",
            }
        )
    interrupted = [summary for summary in scoped if summary.stale]
    if interrupted:
        tiles.append(
            {
                "value": "stale",
                "kind": "warn",
                "count": sum(summary.stale for summary in interrupted),
                "label": (
                    f"interrupted executions · {counted_sweeps(len(interrupted))}"
                ),
            }
        )
    states = Counter(summary.state for summary in scoped)
    for state, count in sorted(states.items()):
        tiles.append(
            {
                "value": f"state:{state}",
                "kind": None,
                "count": count,
                "label": _STATE_TILE_LABELS.get(
                    state, f"{state} {counted_sweeps(count)}"
                ),
            }
        )
    return tiles


def overview_tile_buttons(
    tiles: list[dict[str, Any]], overview_filter: str | None
) -> list[html.Button]:
    """The tile row; the active filter's tile carries the ``on`` mark,
    and clicking it again clears (as does the chip and the seg)."""
    buttons = []
    for tile in tiles:
        classes = "tile"
        if tile["kind"]:
            classes += f" {tile['kind']}"
        if tile["value"] == overview_filter:
            classes += " on"
        buttons.append(
            html.Button(
                [
                    html.Div(str(tile["count"]), className="num"),
                    html.Div(tile["label"], className="lbl"),
                ],
                id={"overview-tile": tile["value"]},
                className=classes,
            )
        )
    return buttons


def overview_filter_matches(summary: SweepSummary, overview_filter: str | None) -> bool:
    """Whether one sweep passes the active tile filter; everything
    passes when no tile is active."""
    if not overview_filter:
        return True
    if overview_filter == "failed":
        return bool(summary.failed)
    if overview_filter == "stale":
        return bool(summary.stale)
    if overview_filter.startswith("state:"):
        return summary.state == overview_filter.removeprefix("state:")
    return True


def overview_filter_chip(filtered_count: int, overview_filter: str) -> html.Div:
    """The visible active-filter chip; its × is the way back."""
    label = _FILTER_CHIP_LABELS.get(
        overview_filter, f"in state {overview_filter.removeprefix('state:')}"
    )
    return html.Div(
        html.Span(
            [
                f"{counted_sweeps(filtered_count)} {label} ",
                html.Button(
                    "\u00d7",
                    id={"overview-filter-clear": "chip"},
                    title="Clear the filter",
                ),
            ],
            className="chip",
        ),
        className="chip-row",
    )


def overview_scope_control(active_count: int, all_count: int, on_all: bool) -> html.Div:
    """The Active/All seg control over the include flags: Active is the
    default discovery scope, All is the project's exhaustive list."""
    return html.Div(
        [
            html.Button(
                f"Active ({active_count})",
                id="overview-scope-active",
                className="on" if not on_all else "",
            ),
            html.Button(
                f"All ({all_count})",
                id="overview-scope-all",
                className="on" if on_all else "",
            ),
        ],
        className="seg",
    )


def overview_actions_bar() -> html.Div:
    """The row-selection action bar; a selection enables Create
    Investigation with the picked sweeps as editor seeds."""
    return html.Div(
        [
            html.Span("", id="overview-selection-count"),
            html.Button(
                "Create Investigation",
                id="overview-create-investigation",
                disabled=True,
                className="btn-primary",
            ),
            html.Button("Clear", id="overview-clear-selection"),
        ],
        id="overview-bulkbar",
        className="bulkbar",
        style={"display": "none"},
    )


def overview_subline(
    summaries: Sequence[SweepSummary], active_count: int, on_all: bool, now_ns: int
) -> html.P:
    """The one-line scope fact: which list is shown, what it hides, and
    the scope's most recent activity."""
    activity = max(
        (
            summary.latest_submitted_ns
            for summary in summaries
            if summary.latest_submitted_ns is not None
        ),
        default=None,
    )
    hidden = len(summaries) - active_count
    if on_all:
        scope = f"All sweeps — {len(summaries)}"
    elif hidden:
        scope = f"Active sweeps — hides {hidden} archived/invalid"
    else:
        scope = "Active sweeps"
    return html.P(
        f"{scope} · last activity "
        + (UNKNOWN if activity is None else components.relative_time(activity, now_ns)),
        className="overview-sub",
    )


def scoped_sweeps(
    summaries: Sequence[SweepSummary], tray: dict[str, Any] | None
) -> list[SweepSummary]:
    """The scope document's sweeps as the overview shows them: picks
    narrow the project, the include flags reveal curated terminal sweeps,
    and incomplete or picked sweeps never drop."""
    scope = tray or {}
    picked = set(scope.get("sweeps") or [])
    include_archived = bool(scope.get("include_archived"))
    include_invalid = bool(scope.get("include_invalid"))
    return [
        summary
        for summary in summaries
        if (not picked or summary.sweep_id in picked)
        and (
            summary.incomplete
            or summary.sweep_id in picked
            or not hidden_curation(
                summary,
                include_archived=include_archived,
                include_invalid=include_invalid,
            )
        )
    ]


def failed_view_section(
    panel: list[Any] | None = None, *, open: bool = False
) -> html.Details:
    """The failure-triage view; the Exceptions tab mounts it open with
    its panel already filled, and a curation action re-renders it."""
    return html.Details(
        [
            html.Summary("Failed executions"),
            dcc.Input(
                id="failed-reason",
                type="text",
                placeholder="Reason (required for Mark invalid)",
                className="reason-input",
            ),
            html.Div(panel or [], id="failed-trials-panel"),
        ],
        id="failed-trials-view",
        open=open,
    )


def failed_view_panel(
    service: DashboardService,
    project: str,
    scoped: Sequence[SweepSummary],
    now_ns: int,
    message: html.Div | None = None,
) -> list[Any]:
    """Children of the failure view: per-sweep groups of failed
    executions — kind and summary without opening each execution, a
    focus link per trial, one mark-invalid action per sweep, and
    select-all/batch controls for invalidating many sweeps at once."""
    rows = service.failed_executions(
        project, [s.sweep_id for s in scoped], limit=_FAILED_VIEW_LIMIT
    )
    children: list[Any] = []
    if message is not None:
        children.append(message)
    names = {s.sweep_id: s.name for s in scoped}
    by_sweep: dict[str, list[FailedExecutionRow]] = {}
    for row in rows:
        by_sweep.setdefault(row.sweep_id, []).append(row)
    if by_sweep:
        children.append(
            html.Div(
                [
                    dcc.Checklist(
                        id="failed-select-all",
                        options=[{"label": "Select all failed sweeps", "value": "all"}],
                        value=[],
                    ),
                    html.Button(
                        "Mark selected invalid",
                        id="failed-invalid-batch",
                        className="action",
                    ),
                ],
                className="failed-controls",
            )
        )
    for sweep_id, group in by_sweep.items():
        children.append(
            html.Div(
                [
                    html.P(
                        [
                            focus_button(
                                names.get(sweep_id, short_id(sweep_id)),
                                "sweep",
                                sweep_id,
                            ),
                            dcc.Checklist(
                                id={"failed-sweep": sweep_id},
                                options=[{"label": "", "value": sweep_id}],
                                value=[],
                                className="failed-sweep-check",
                            ),
                            html.Button(
                                "Mark sweep invalid",
                                id={"failed-invalid": sweep_id},
                                className="action",
                            ),
                        ],
                        className="failed-sweep-head",
                    ),
                    components.DataTable(
                        ("Trial", "#", "Kind", "Summary", "Last activity"),
                        [
                            (
                                focus_button(
                                    f"#{row.trial_number}", "trial", row.trial_id
                                ),
                                row.trial_number,
                                row.failure_kind or UNKNOWN,
                                row.failure_summary or MISSING,
                                components.relative_time(row.updated_ns, now_ns),
                            )
                            for row in group
                        ],
                    ),
                ],
                className="failed-sweep",
            )
        )
    if not rows:
        children.append(components.Empty("No failed executions in scope."))
    elif len(rows) >= _FAILED_VIEW_LIMIT:
        children.append(
            html.P("Showing the most recent; narrow the scope.", className="hint")
        )
    return children


def overview_tab(
    service: DashboardService,
    project: str | None,
    tray: dict[str, Any] | None,
    *,
    overview_filter: str | None = None,
    sort: list | None = None,
) -> html.Div:
    """The operational overview: every tile is a working filter over the
    paginated sweep table, the Active/All control carries the project's
    exhaustive list, and a row click inspects the sweep. Per-sweep depth
    lives in the inspector; the include flags curate discovery
    (jernerics-mqw, jernerics-g5rw.7)."""
    if not project:
        return html.Div(
            components.Empty("Pick a project in the header to browse its sweeps.")
        )
    summaries = service.sweep_overview(project)
    if not summaries:
        return html.Div(
            components.Empty(f"No sweeps tracked for project {project} yet.")
        )
    scoped = scoped_sweeps(summaries, tray)
    if not scoped:
        if (tray or {}).get("sweeps"):
            return html.Div(
                components.Empty(f"No picked sweeps remain in project {project}.")
            )
        return html.Div(
            components.Empty(
                f"No current sweeps in project {project}; archived or invalid "
                "sweeps stay hidden until the scope includes them."
            )
        )
    now = time.time_ns()
    on_all = bool(
        (tray or {}).get("include_archived") or (tray or {}).get("include_invalid")
    )
    active_count = len(scoped_sweeps(summaries, {}))
    filtered = [
        summary
        for summary in scoped
        if overview_filter_matches(summary, overview_filter)
    ]
    rows = sort_rows(overview_sweep_rows(filtered), _OVERVIEW_SWEEP_COLUMNS, sort)
    return html.Div(
        [
            overview_subline(summaries, active_count, on_all, now),
            html.Div(
                overview_tile_buttons(overview_tiles(scoped), overview_filter),
                className="tiles",
            ),
            overview_scope_control(active_count, len(summaries), on_all),
            *(
                [overview_filter_chip(len(filtered), overview_filter)]
                if overview_filter
                else []
            ),
            overview_actions_bar(),
            html.Section(
                [
                    html.H3("Sweeps"),
                    AgGrid(
                        id={"overview-grid": "sweeps"},
                        rowData=rows,
                        columnDefs=sortable_columns(_OVERVIEW_SWEEP_COLUMNS, sort),
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
                    ),
                    html.P(
                        "Checkboxes pick sweeps for Create Investigation; a row "
                        "click inspects that sweep in the inspector. Tiles filter "
                        "the table; Active/All chooses between current sweeps and "
                        "every sweep of the project.",
                        className="hint",
                    ),
                ],
                className="section overview-sweeps",
            ),
        ]
    )


def investigation_coverage_text(row: InvestigationRow) -> str:
    """The one-line coverage summary: with outcome / incomplete / invalid."""
    incomplete = row.member_count - row.completed
    return (
        f"{row.with_outcome} with outcome · {incomplete} incomplete · "
        f"{row.invalid} invalid"
    )


def _investigation_index_rows(
    project: str, rows: Sequence[InvestigationRow]
) -> list[dict[str, Any]]:
    return [
        {
            "investigation_id": row.investigation_id,
            "name": row.name,
            "link_href": (
                f"{ROUTES_BASE}/project/{project}/investigation/" + row.investigation_id
            ),
            "link_label": row.name,
            "factor": row.factor,
            "outcome": row.outcome,
            "member_count": row.member_count,
            "coverage": investigation_coverage_text(row),
            "last_activity_ns": row.last_activity_ns,
            "edit_members": "",
            "edit_href": (
                f"{ROUTES_BASE}/project/{project}/investigation/"
                + row.investigation_id
                + "/edit"
            ),
        }
        for row in rows
    ]


_INVESTIGATION_COLUMNS: list[SortColumn] = [
    SortColumn(
        "name",
        "Investigation",
        "string",
        definition={"cellRenderer": {"function": "renderLinkCell(params)"}},
    ),
    SortColumn("factor", "Factor", "string"),
    SortColumn("outcome", "Outcome", "string"),
    SortColumn("member_count", "Members", "numeric"),
    SortColumn("coverage", "Coverage", "string"),
    SortColumn(
        "last_activity_ns",
        "Last activity",
        "ns",
        definition={"valueFormatter": {"function": "renderRelative(x)"}},
    ),
    SortColumn(
        "edit_members",
        "",
        "string",
        definition={
            "sortable": False,
            "cellRenderer": {"function": "renderEditCell(params)"},
        },
    ),
]


_UNORGANIZED_COLUMNS: list[SortColumn] = [
    SortColumn(
        "name",
        "Sweep",
        "string",
        definition={"cellRenderer": {"function": "renderLinkCell(params)"}},
    ),
    SortColumn("state", "State", "string"),
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


def investigations_tab(service: DashboardService, project: str | None) -> html.Div:
    """The Investigations index: one row per investigation with its real
    coverage facts, the project-scope Unorganized list, and the New
    Investigation action into the (task .8) member editor."""
    if not project:
        return html.Div(
            components.Empty(
                "Pick a project in the header to browse its investigations."
            )
        )
    try:
        index_rows = service.investigations_index(project)
        unorganized = service.unorganized(project)
    except CurationUnavailableError as error:
        return html.Div(components.Empty(str(error)))
    unorganized_rows = sort_rows(
        [
            {
                "sweep_id": summary.sweep_id,
                "name": summary.name,
                "link_href": analysis.workspace_focus_href(
                    project, "sweep", summary.sweep_id
                ),
                "link_label": summary.name,
                "state": summary.state,
                "expected_trials": summary.expected_trials,
                "last_activity_ns": summary.latest_submitted_ns,
            }
            for summary in unorganized
        ],
        _UNORGANIZED_COLUMNS,
        None,
    )
    return html.Div(
        [
            html.Div(
                html.A(
                    "New Investigation",
                    href=f"{ROUTES_BASE}/project/{project}/investigation/new",
                    className="btn btn-primary",
                ),
                className="actions",
            ),
            html.Section(
                (
                    [
                        AgGrid(
                            id="investigations-grid",
                            rowData=_investigation_index_rows(project, index_rows),
                            columnDefs=sortable_columns(_INVESTIGATION_COLUMNS),
                            defaultColDef=_GRID_DEFAULTS,
                            dashGridOptions=components.grid_options(),
                            getRowId="params.data.investigation_id",
                            className="ag-theme-alpine grid",
                        )
                    ]
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
            html.Section(
                [
                    html.H3("Unorganized"),
                    html.P(
                        f"{counted_sweeps(len(unorganized))} not in any Investigation",
                        className="overview-sub",
                    ),
                    *(
                        [
                            html.Details(
                                [
                                    html.Summary("Show list"),
                                    AgGrid(
                                        id="unorganized-grid",
                                        rowData=unorganized_rows,
                                        columnDefs=sortable_columns(
                                            _UNORGANIZED_COLUMNS
                                        ),
                                        defaultColDef=_GRID_DEFAULTS,
                                        dashGridOptions=components.grid_options(),
                                        getRowId=_SWEEP_ROW_ID,
                                        className="ag-theme-alpine grid",
                                    ),
                                ],
                                className="failgroup",
                            )
                        ]
                        if unorganized
                        else []
                    ),
                ],
                className="section investigations-unorganized",
            ),
        ],
    )


def exceptions_tab(
    service: DashboardService,
    project: str | None,
    tray: dict[str, Any] | None,
    now_ns: int,
) -> html.Div:
    """Project-scoped failure triage: the failure view, mounted open
    with its panel already filled (the Overview tiles filter instead of
    opening it)."""
    if not project:
        return html.Div(
            components.Empty("Pick a project in the header to browse its exceptions.")
        )
    scoped = scoped_sweeps(service.sweep_overview(project), tray)
    if not any(summary.failed for summary in scoped):
        return html.Div(
            components.Empty(f"No failed executions in project {project}."),
            className="section exceptions-view",
        )
    return html.Div(
        failed_view_section(
            failed_view_panel(service, project, scoped, now_ns), open=True
        ),
        className="section exceptions-view",
    )


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


def workspace_page(
    project: str,
    *,
    sort: list | None = None,
    quick: str = "",
    filters: dict | None = None,
) -> html.Div:
    """The stable workspace: scope browser, tabbed canvas, inspector.

    Mounted once per project navigation; every data region updates through
    its own callback so scope, tab, settings, and focus survive refreshes
    and history without remounting children.
    """
    return html.Div(
        [
            html.H2(f"Project {project}"),
            html.Div(id={"analysis-error": "page"}),
            dcc.Store(id="inspector-render-store"),
            dcc.Store(id="sweep-browser-facts-store"),
            dcc.Store(id="trial-browser-facts-store"),
            dcc.Store(id="scroll-restore-store"),
            html.Div(
                [
                    html.Details(
                        [
                            html.Summary("Browse scope"),
                            html.Div(id="analysis-scope-bar", className="scope-bar"),
                            dcc.Checklist(
                                id="analysis-include",
                                options=[
                                    {
                                        "label": " include archived sweeps",
                                        "value": "archived",
                                    },
                                    {
                                        "label": " include invalid sweeps",
                                        "value": "invalid",
                                    },
                                ],
                                value=[],
                                className="include-toggle",
                            ),
                            dcc.Input(
                                id="workspace-quick",
                                value=quick,
                                type="search",
                                placeholder="Search sweeps…",
                                className="quick-filter",
                            ),
                            AgGrid(
                                id="sweep-grid",
                                rowData=[],
                                columnDefs=browser_sweep_columns(sort),
                                defaultColDef=_GRID_DEFAULTS,
                                dashGridOptions=components.grid_options(
                                    rowSelection={"mode": "multiRow"},
                                    quickFilterText=quick,
                                ),
                                getRowId=_SWEEP_ROW_ID,
                                filterModel=filters,
                                className="ag-theme-alpine grid",
                            ),
                            html.Div(
                                "", id="workspace-curation-note", className="hint"
                            ),
                            curation_panel(),
                            AgGrid(
                                id="analysis-family-grid",
                                rowData=[],
                                columnDefs=[],
                                defaultColDef=_GRID_DEFAULTS,
                                dashGridOptions=components.grid_options(
                                    rowSelection={"mode": "multiRow"},
                                    rowClassRules={
                                        "row-hover-emphasis": (
                                            "params.data && params.data._hovered"
                                        )
                                    },
                                ),
                                getRowId=_TRIAL_ROW_ID,
                                className="ag-theme-alpine grid trial-browser",
                            ),
                            dcc.Checklist(
                                id="analysis-expand",
                                options=[
                                    {
                                        "label": " include retry families — expand "
                                        "picked roots to every generation",
                                        "value": "expand",
                                    }
                                ],
                                value=[],
                                className="expand-toggle",
                            ),
                            html.P(
                                "Sweep checkboxes edit the scope; a row click "
                                "inspects that sweep. Trial checkboxes pick "
                                "retry roots.",
                                className="hint",
                            ),
                        ],
                        id="scope-browser",
                        className="scope-browser",
                        open=True,
                    ),
                    html.Div(
                        [
                            dcc.Tabs(
                                id={"analysis-tabs": "canvas"},
                                value="overview",
                                children=[
                                    dcc.Tab(label="Overview", value="overview"),
                                    dcc.Tab(
                                        label="Investigations", value="investigations"
                                    ),
                                    dcc.Tab(label="Exceptions", value="exceptions"),
                                ],
                            ),
                            html.Div(
                                id="workspace-overview", style={"display": "block"}
                            ),
                            html.Div(
                                id="workspace-investigations", style={"display": "none"}
                            ),
                            html.Div(
                                id="workspace-exceptions", style={"display": "none"}
                            ),
                        ],
                        className="workspace-canvas",
                    ),
                    html.Aside(
                        id="inspector",
                        className="inspector",
                        children=inspector_placeholder(),
                    ),
                ],
                className="workspace-main",
            ),
        ],
        className="page workspace",
    )


def focus_incomplete(service: DashboardService, focus: dict[str, Any] | None) -> bool:
    """Whether the focused object still has work in flight."""
    if not focus:
        return False
    kind, object_id = focus.get("kind"), str(focus.get("id") or "")
    if kind == "sweep":
        return service.sweep_incomplete(object_id)
    if kind == "trial":
        detail = service.trial_detail(object_id)
        return detail is not None and any(
            record.ended_at is None for record in detail.executions
        )
    if kind == "execution":
        detail = service.execution_detail(object_id)
        return detail is not None and detail.context["ended_ns"] is None
    return False
