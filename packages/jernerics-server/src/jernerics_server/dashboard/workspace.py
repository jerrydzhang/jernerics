import json
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

from dash import dcc, html
from dash_ag_grid import AgGrid
from jernerics_schema import ExecutionRecord

from . import analysis, artifacts, components
from .components import MISSING, UNKNOWN, Badge, short_id, time_cell
from .service import (
    DashboardService,
    ExecutionDetail,
    FamilyRow,
    SweepDetail,
    SweepSummary,
    TrialDetail,
)

FOCUS_KINDS = ("sweep", "trial", "execution")


_INCOMPLETE_TRIAL_STATES = ("waiting", "running")

_MONITORING_ORDER = ("active", "quiet", "stale", "failed", "succeeded", UNKNOWN)

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
    """The active-work note when incomplete sweeps carry curation."""
    for row in rows or []:
        if row.get("curation"):
            return (
                "Curation does not cancel or hide active work — incomplete "
                "sweeps stay visible and selectable while they run."
            )
    return ""


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
    for sweep_id, name in zip(picked_ids, picked, strict=True):
        summary = summaries.get(sweep_id)
        if summary is None:
            continue
        if summary.archived:
            children.append(Badge(f"{name} archived", kind="archived"))
        if summary.invalid:
            children.append(Badge(f"{name} invalid", kind="invalid"))
            children.append(
                html.Span(
                    f"{name} is marked scientifically invalid — reason: "
                    f"{summary.invalid_reason}. Continue only with that "
                    "in mind, or remove it from the scope.",
                    className="scope-warning",
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
                "Mark invalid",
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
            [html.H3("Executions"), _executions_table(detail.executions, now_ns)],
            className="section",
        ),
        html.Section(
            [html.H3("In-flight progress"), _progress_list(detail.progress)],
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
        dcc.Store(id="family-lineage-store", data={"lineage": detail.lineage}),
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


def inspector_content(
    service: DashboardService, focus: dict[str, Any] | None, now_ns: int
) -> html.Div:
    """The focused object's factual content; a missing id is named, not
    hidden."""
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
    elif kind == "trial":
        detail = service.trial_detail(object_id)
        body = (
            _trial_sections(detail, now_ns)
            if detail is not None
            else [components.Empty(f"No trial matches {object_id} in this store.")]
        )
        heading = f"Trial {short_id(object_id)}"
    elif kind == "execution":
        detail = service.execution_detail(object_id)
        body = (
            _execution_sections(detail, now_ns)
            if detail is not None
            else [components.Empty(f"No execution matches {object_id} in this store.")]
        )
        heading = f"Execution {short_id(object_id)}"
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


_OVERVIEW_SWEEP_COLUMNS: list[dict[str, Any]] = [
    {"headerName": "Sweep", "field": "name"},
    {"headerName": "State", **_badge_cell("state")},
    {"headerName": "Health", **_badge_cell("health")},
    {
        "headerName": "Monitoring",
        "field": "monitoring",
        **components.clamped_column(),
        "maxWidth": 320,
    },
    {"headerName": "Curation", "field": "curation"},
    {"headerName": "Expected trials", "field": "expected_trials"},
    {"headerName": "Last activity", "field": "last_activity"},
]


def _monitoring_summary(summary: SweepSummary) -> str:
    """Compact nonzero monitoring text for one overview grid row."""
    parts = [
        f"{label} {getattr(summary, label)}"
        for label in _MONITORING_ORDER
        if getattr(summary, label)
    ]
    return " · ".join(parts) if parts else MISSING


def overview_sweep_rows(
    scoped: Sequence[SweepSummary], now_ns: int | None = None
) -> list[dict[str, Any]]:
    """One overview-grid row per scoped sweep; deep detail stays in the
    inspector behind a row click."""
    now = time.time_ns() if now_ns is None else now_ns
    return [
        {
            "sweep_id": summary.sweep_id,
            "name": summary.name,
            "state": summary.state,
            "health": summary.health,
            "monitoring": _monitoring_summary(summary),
            "curation": sweep_curation(summary),
            "expected_trials": (
                MISSING if summary.expected_trials is None else summary.expected_trials
            ),
            "last_activity": (
                MISSING
                if summary.latest_submitted_ns is None
                else components.relative_time(summary.latest_submitted_ns, now)
            ),
        }
        for summary in scoped
    ]


def overview_rollup(scoped: Sequence[SweepSummary], now_ns: int) -> html.Section:
    """Aggregate operational facts for the scoped sweeps: counts by
    state and health, execution monitoring totals, in-flight
    executions, and the most recent activity."""
    states = Counter(summary.state for summary in scoped)
    healths = Counter(summary.health for summary in scoped)
    monitoring = {
        label: sum(getattr(summary, label) for summary in scoped)
        for label in _MONITORING_ORDER
    }
    in_flight = sum(max(0, summary.started - summary.terminal) for summary in scoped)
    activity = max(
        (
            summary.latest_submitted_ns
            for summary in scoped
            if summary.latest_submitted_ns is not None
        ),
        default=None,
    )
    return html.Section(
        [
            html.H3("Scope roll-up"),
            html.P(
                [
                    html.Span(f"sweeps {len(scoped)}"),
                    *(
                        Badge(f"{state} {count}", kind=state)
                        for state, count in sorted(states.items())
                    ),
                ],
                className="fact-row",
            ),
            html.P(
                [
                    Badge(f"health {label} {count}", kind=label)
                    for label, count in sorted(healths.items())
                ],
                className="fact-row",
            ),
            html.P(
                [
                    *_monitoring_badges(monitoring),
                    html.Span(f"in-flight executions {in_flight}"),
                    html.Span(
                        "last activity "
                        + (
                            UNKNOWN
                            if activity is None
                            else components.relative_time(activity, now_ns)
                        )
                    ),
                ],
                className="fact-row",
            ),
        ],
        className="section overview-rollup",
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


def overview_tab(
    service: DashboardService, project: str | None, tray: dict[str, Any] | None
) -> html.Div:
    """Bounded operational summary for the scope: an aggregate roll-up
    plus one virtualized grid row per sweep. Per-sweep depth lives in
    the inspector; an empty scope means the whole project, curated by
    the Browse include toggles (jernerics-mqw)."""
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
    return html.Div(
        [
            overview_rollup(scoped, now),
            html.Section(
                [
                    html.H3("Sweeps in scope"),
                    AgGrid(
                        id="overview-sweep-grid",
                        rowData=overview_sweep_rows(scoped, now),
                        columnDefs=_OVERVIEW_SWEEP_COLUMNS,
                        defaultColDef=_GRID_DEFAULTS,
                        dashGridOptions=components.grid_options(),
                        getRowId=_SWEEP_ROW_ID,
                        className="ag-theme-alpine grid",
                    ),
                    html.P(
                        "A row click inspects that sweep in the inspector; the "
                        "grid virtualizes, so any scope size renders as one "
                        "bounded region.",
                        className="hint",
                    ),
                ],
                className="section overview-sweeps",
            ),
        ]
    )


def _series_tab() -> html.Div:
    return html.Div(
        [
            html.Div(
                [
                    dcc.Dropdown(
                        id="analysis-key",
                        placeholder="Value keys…",
                        multi=True,
                        searchable=True,
                    ),
                    dcc.RadioItems(
                        id="analysis-mode",
                        options=[
                            {"label": " Stacked", "value": "stacked"},
                            {"label": " Overlay", "value": "overlay"},
                        ],
                        value="stacked",
                        inline=True,
                    ),
                    dcc.Dropdown(id="analysis-color", placeholder="Color by…"),
                    dcc.Dropdown(
                        id="analysis-facet",
                        placeholder="Facet rows…",
                    ),
                    dcc.RadioItems(
                        id="analysis-reduction",
                        options=[
                            {"label": f" {name}", "value": name}
                            for name in analysis.ANALYSIS_REDUCTIONS
                        ],
                        value="none",
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
                        value="all",
                        inline=True,
                    ),
                    html.Button("Refresh", id="analysis-refresh", n_clicks=0),
                    dcc.Checklist(
                        id="analysis-auto-refresh",
                        options=[
                            {
                                "label": " Auto-refresh while incomplete",
                                "value": "auto",
                            }
                        ],
                        value=[],
                        inline=True,
                    ),
                    html.Span(id="analysis-series-status", className="series-status"),
                    html.Span(id="analysis-updated", className="series-updated"),
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
            html.Div(id="analysis-context-filters", className="context-filters"),
            html.Div(id="analysis-series-panels"),
            dcc.Graph(id="analysis-series-figure", clear_on_unhover=True),
            dcc.Store(id="analysis-series-figure-store"),
            dcc.Store(id="analysis-series-data"),
            dcc.Store(id="analysis-refresh-store"),
        ]
    )


def _optuna_tab() -> html.Div:
    return html.Div(
        [
            html.P(
                "One figure set per selected sweep; contour needs two numeric params.",
                className="hint",
            ),
            html.Div(
                [
                    dcc.Dropdown(id="analysis-contour-x", placeholder="Contour x…"),
                    dcc.Dropdown(id="analysis-contour-y", placeholder="Contour y…"),
                ],
                className="analysis-controls",
            ),
            html.Div(id="analysis-optuna"),
        ]
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
            html.Div(id="analysis-error"),
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
                                id="analysis-tabs",
                                value="overview",
                                children=[
                                    dcc.Tab(label="Overview", value="overview"),
                                    dcc.Tab(label="Catalog", value="catalog"),
                                    dcc.Tab(label="Series", value="series"),
                                    dcc.Tab(label="Points", value="points"),
                                    dcc.Tab(label="Optuna", value="optuna"),
                                    dcc.Tab(label="Python", value="python"),
                                ],
                            ),
                            html.Div(
                                id="workspace-overview", style={"display": "block"}
                            ),
                            html.Div(id="analysis-catalog", style={"display": "none"}),
                            html.Div(
                                _series_tab().children,
                                id="analysis-series-tab",
                                style={"display": "none"},
                            ),
                            html.Div(id="analysis-points", style={"display": "none"}),
                            html.Div(
                                _optuna_tab().children,
                                id="analysis-optuna-tab",
                                style={"display": "none"},
                            ),
                            html.Div(id="analysis-python", style={"display": "none"}),
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
        detail = service.sweep_detail(object_id)
        return detail is not None and detail.overview.incomplete
    if kind == "trial":
        detail = service.trial_detail(object_id)
        return detail is not None and any(
            record.ended_at is None for record in detail.executions
        )
    if kind == "execution":
        detail = service.execution_detail(object_id)
        return detail is not None and detail.context["ended_ns"] is None
    return False
