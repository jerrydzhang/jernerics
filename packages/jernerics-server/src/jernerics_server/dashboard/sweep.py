"""The per-sweep page: ``/dashboard/project/<name>/sweep/<sweep-id>``.

Prototype-faithful composition (jernerics-proto ``build.py::sweep_page``):
breadcrumb with the ``?via=`` investigation return, the sweep heading with
curation badges, the data-supported sub-nav, provenance, and the
Executions/Trials/Params tables. Trial rows expand in place to their
params, value catalog, and lineage; their checkboxes pick retry roots.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs

from dash import dcc, html
from dash.development.base_component import Component
from jernerics_schema import (
    ArtifactRecord,
    ExecutionRecord,
    ProvenanceRecord,
    TrialParamRecord,
    TrialRecord,
    ValueCatalogRecord,
)

from jernerics_server.investigations import InvestigationRecord

from . import analysis, components, sweep_views, workspace
from .page import (
    artifact_chips,
    breadcrumbs,
    head_cell,
    limit_row,
    page_shell,
    scroll_table,
    status_dot,
)
from .routes import ROUTES_BASE
from .service import (
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
    SweepSummary,
)

_COMPLETED = "completed"
_FAILED = "failed"


def via_from_search(search: str | None) -> str | None:
    """The ``?via=`` investigation id a return path carries, if any."""
    values = parse_qs((search or "").lstrip("?")).get("via")
    return values[0] if values else None


@dataclass(frozen=True)
class SweepPageData:
    """Everything the sweep page renders, fetched once per fact change."""

    context: dict[str, Any]
    overview: SweepSummary
    provenance: list[ProvenanceRecord]
    jobs: list[dict[str, Any]]
    executions: list[ExecutionRecord]
    trials: list[TrialRecord]
    params: list[TrialParamRecord]
    artifacts: list[ArtifactRecord]
    catalogs: dict[str, list[ValueCatalogRecord]]
    lineage: list[dict[str, Any]]
    via_record: InvestigationRecord | None
    series_supported: bool


def collect(
    service: DashboardService, sweep_id: str, via: str | None
) -> SweepPageData | None:
    """The page's facts for one sweep; ``None`` when no sweep matches."""
    detail = service.sweep_detail(sweep_id)
    if detail is None:
        return None
    project = str(detail.context["project"])
    trials = service.sweep_trials(sweep_id)
    return SweepPageData(
        context=detail.context,
        overview=detail.overview,
        provenance=service.sweep_provenance(sweep_id),
        jobs=detail.jobs,
        executions=detail.executions,
        trials=trials,
        params=service.sweep_trial_params(sweep_id),
        artifacts=service.sweep_artifacts(sweep_id),
        catalogs=service.trial_value_catalogs(
            project, [str(trial.trial_id) for trial in trials]
        ),
        lineage=detail.lineage,
        via_record=_via_investigation(service, project, via),
        series_supported=any(
            entry["kind"] == "scalar" and entry["steps"]
            for entry in service.analysis_value_keys(project, {"sweeps": [sweep_id]})
        ),
    )


def facts(data: SweepPageData) -> dict[str, Any]:
    """Digest-stable stored facts — no rendered tree, nothing
    wall-clock derived — so a tick without a fact change ships nothing."""
    return {
        "context": data.context,
        "overview": asdict(data.overview),
        "provenance": [
            (str(row.submission_id), row.submitted_at_ns, row.expected_trials)
            for row in data.provenance
        ],
        "jobs": [
            (
                str(job["submission_id"]),
                str(job["job_id"]),
                str(job["scheduler_job_id"]),
                str(job["role"]),
            )
            for job in data.jobs
        ],
        "executions": [
            (
                str(row.execution_id),
                str(row.trial_id),
                row.hostname,
                components.datetime_to_ns(row.started_at),
                (
                    None
                    if row.ended_at is None
                    else components.datetime_to_ns(row.ended_at)
                ),
                str(row.outcome) if row.outcome else "",
                row.failure_kind.value if row.failure_kind else "",
                row.last_heartbeat_ns,
                str(row.monitoring) if row.monitoring else "",
            )
            for row in data.executions
        ],
        "trials": [
            (
                str(row.trial_id),
                row.number,
                str(row.state),
                row.objective,
                row.retry_index,
                str(row.retry_root_trial_id),
                bool(row.distributions),
            )
            for row in data.trials
        ],
        "params": [
            (str(row.trial_id), row.kind, row.key, str(row.value))
            for row in data.params
        ],
        "artifacts": [
            (str(row.artifact_id), str(row.trial_id), row.key, row.filename)
            for row in data.artifacts
        ],
        "catalogs": {
            trial_id: [
                (row.key, row.kind, row.n_points, row.latest_step, row.n_trials)
                for row in catalog
            ]
            for trial_id, catalog in data.catalogs.items()
        },
        "lineage": [
            (row["trial_id"], row["root"], row["index"]) for row in data.lineage
        ],
        "via": None if data.via_record is None else str(data.via_record.id),
        "series_supported": data.series_supported,
    }


def digest(data: SweepPageData) -> str:
    """Short stable digest of the page's stored facts."""
    payload = json.dumps(facts(data), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _via_investigation(
    service: DashboardService, project: str, via: str | None
) -> InvestigationRecord | None:
    """The investigation a ``via`` return path names, when it exists in
    this store and belongs to the same project; anything else is an
    unknown context and renders none."""
    if not via:
        return None
    try:
        record = service.investigation_detail(via).investigation
    except (CurationRejectedError, CurationUnavailableError):
        return None
    return record if record.project == project else None


def _sig(value: float | None, digits: int = 4) -> str:
    return components.MISSING if value is None else f"{value:.{digits}g}"


def _param_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _numbers(data: SweepPageData) -> dict[str, int]:
    return {str(row.trial_id): row.number for row in data.trials}


def _lost_executions(data: SweepPageData) -> list[ExecutionRecord]:
    """Executions that never reached a terminal outcome and went quiet."""
    return [
        row
        for row in data.executions
        if row.outcome is None and row.monitoring == "stale"
    ]


def _failed_trial_ids(data: SweepPageData) -> set[str]:
    """Trials whose latest execution ended in failure; executions
    arrive oldest first, so the last outcome per trial wins."""
    outcomes: dict[str, str | None] = {
        str(row.trial_id): row.outcome for row in data.executions
    }
    return {trial_id for trial_id, outcome in outcomes.items() if outcome == "failure"}


def _trial_state_dot(
    trial: TrialRecord, lost_numbers: set[int], failed_ids: set[str]
) -> Component:
    if trial.state == _COMPLETED:
        return status_dot("completed")
    if trial.state == _FAILED:
        return status_dot("failed")
    if str(trial.trial_id) in failed_ids:
        return status_dot("failed", "failed execution")
    if trial.number in lost_numbers:
        return status_dot("stale", "lost execution")
    return status_dot("running")


def _last_activity_ns(executions: list[ExecutionRecord]) -> int | None:
    stamps = [
        (None if row.ended_at is None else components.datetime_to_ns(row.ended_at))
        or row.last_heartbeat_ns
        or components.datetime_to_ns(row.started_at)
        for row in executions
    ]
    return max(stamps) if stamps else None


def _breadcrumbs(data: SweepPageData, project: str) -> html.Div:
    trail: list[tuple[str, str] | str] = [
        ("Projects", f"{ROUTES_BASE}/"),
        (project, f"{ROUTES_BASE}/project/{project}"),
    ]
    if data.via_record is None:
        trail.append("sweep")
    else:
        trail.append(("Investigations", workspace.investigations_index_href(project)))
        trail.append(
            (
                data.via_record.name,
                analysis.investigation_view_href(
                    project, str(data.via_record.id), "compare"
                ),
            )
        )
    trail.append(data.context["name"])
    return breadcrumbs(trail)


def _heading(data: SweepPageData) -> html.H1:
    bits: list[Component | str] = [data.context["name"]]
    if data.overview.invalid:
        bits.append(html.Span("invalid", className="badge invalid"))
        bits.append(
            html.Span(f"reason: {data.overview.invalid_reason}", className="annotate")
        )
    if data.overview.archived:
        bits.append(html.Span("archived", className="badge archived"))
    return html.H1(bits)


def _sub_line(data: SweepPageData, now_ns: int) -> html.P:
    lost = _lost_executions(data)
    complete = sum(1 for row in data.trials if row.state == _COMPLETED)
    parts: list[Component | str] = [
        status_dot(data.overview.state, f"{complete}/{len(data.trials)} trials"),
        f" · {data.overview.succeeded} succeeded · {data.overview.failed} failed",
    ]
    if lost:
        parts.append(" · ")
        parts.append(
            html.Span(f"{len(lost)} lost — no terminal event", className="warn-text")
        )
    activity = _last_activity_ns(data.executions)
    parts.append(
        " · last activity "
        + (
            components.relative_time(activity, now_ns)
            if activity is not None
            else "never"
        )
    )
    return html.P(parts, className="sub")


def _views_row(
    project: str, sweep_id: str, data: SweepPageData, view: str | None
) -> html.Div:
    """The sub-nav: Overview plus the analysis sub-views the sweep's
    real data supports, as ``?view=`` links on this page. A ``via``
    investigation keeps R4's member-scoped destinations for
    Series/Points/Search — only Optuna, which no investigation view
    covers, opens the sweep-page sub-view."""
    points_supported = data.overview.started > 0
    optuna_supported = any(row.distributions for row in data.trials)
    search_supported = bool(data.trials)
    via = data.via_record

    def scoped(view_name: str) -> str | None:
        if via is None or view_name == "optuna":
            return sweep_views.sweep_href(project, sweep_id, view_name)
        # Series and Points narrow to this member; Search stays
        # cohort-scoped (R4's pinned destination).
        member = sweep_id if view_name in ("series", "points") else None
        return analysis.investigation_view_href(project, str(via.id), view_name, member)

    def entry(label: str, view_name: str, supported: bool) -> Component | None:
        if not supported:
            return None
        return html.A(
            label,
            href=scoped(view_name),
            className="on" if view == view_name else None,
        )

    if view is None:
        views: list[Component] = [html.Span("Overview", className="on")]
    else:
        views: list[Component] = [
            html.A("Overview", href=sweep_views.sweep_href(project, sweep_id))
        ]
    for label, view_name, supported in (
        ("Series", "series", data.series_supported),
        ("Points", "points", points_supported),
        ("Search", "search", search_supported),
        ("Optuna", "optuna", optuna_supported),
    ):
        item = entry(label, view_name, supported)
        if item is not None:
            views.append(item)
    return limit_row(
        html.Span("Views", className="annotate"), html.Div(views, className="seg")
    )


def _actions_row(data: SweepPageData, project: str, sweep_id: str) -> html.Div:
    offered = workspace.curation_transitions(
        data.overview.archived, data.overview.invalid
    )
    actions: list[Component | str] = []
    if offered["invalid"]:
        actions.append(
            html.Button("Mark invalid", id="sweep-invalid", className="btn-danger")
        )
        actions.append(
            dcc.Input(
                id="sweep-reason",
                type="text",
                placeholder="Reason (required for Mark invalid)",
            )
        )
    if offered["archive"]:
        actions.append(html.Button("Archive", id="sweep-archive"))
    if offered["restore_validity"]:
        actions.append(html.Button("Clear invalid", id="sweep-restore-validity"))
    if offered["restore"]:
        actions.append(html.Button("Unarchive", id="sweep-restore"))
    if data.overview.failed:
        actions.append(
            html.A(
                "Failures in this sweep",
                href=f"{ROUTES_BASE}/project/{project}/exceptions?sweep={sweep_id}",
            )
        )
    if data.via_record is not None:
        actions.append(
            html.A(
                f"← {data.via_record.name}",
                href=analysis.investigation_view_href(
                    project, str(data.via_record.id), "compare"
                ),
            )
        )
    actions.append(html.Div(id="sweep-message"))
    return html.Div(actions, className="actions")


def _provenance_grid(data: SweepPageData, now_ns: int) -> html.Div | str:
    first = data.provenance[0] if data.provenance else None
    if first is None:
        return ""
    jobs = ", ".join(
        f"{job['scheduler_job_id']} ({job['role']})"
        for job in data.jobs
        if job.get("scheduler_job_id")
    )

    def cell(label: str, value: Component | str) -> html.Div:
        return html.Div([html.Span(label), html.B(value)])

    return html.Div(
        [
            cell("Backend", first.backend),
            cell("Git", (first.git_hash or "")[:8] or components.MISSING),
            cell("Config", first.config_source or components.MISSING),
            cell(
                "Expected trials",
                (
                    components.MISSING
                    if first.expected_trials is None
                    else str(first.expected_trials)
                ),
            ),
            cell("Scheduler jobs", jobs or components.MISSING),
            cell(
                "Submitted",
                (
                    components.MISSING
                    if first.submitted_at_ns is None
                    else components.relative_time(first.submitted_at_ns, now_ns)
                ),
            ),
        ],
        className="meta-grid",
    )


def _outcome_dot(
    row: ExecutionRecord, lost: list[ExecutionRecord], now_ns: int
) -> Component:
    outcome = row.outcome or ""
    if outcome == "success":
        return status_dot("completed", "success")
    if outcome == "failure":
        return status_dot("failed", row.failure_kind.value if row.failure_kind else "")
    if row in lost:
        return status_dot(
            "stale",
            "lost — last heartbeat "
            + components.relative_time(row.last_heartbeat_ns, now_ns),
        )
    return status_dot("running")


def _execution_cells(
    row: ExecutionRecord,
    numbers: dict[str, int],
    lost: list[ExecutionRecord],
    now_ns: int,
) -> html.Tr:
    started = components.datetime_to_ns(row.started_at)
    ended = None if row.ended_at is None else components.datetime_to_ns(row.ended_at)
    return html.Tr(
        [
            html.Td(
                f"#{numbers.get(str(row.trial_id), components.MISSING)}",
                className="num",
            ),
            html.Td(components.short_id(str(row.execution_id)), className="mono"),
            html.Td(row.hostname or components.MISSING),
            html.Td(_outcome_dot(row, lost, now_ns)),
            html.Td(
                components.relative_time(started, now_ns),
                title=components.absolute_time(started),
                className="num",
            ),
            html.Td(
                (
                    components.MISSING
                    if ended is None
                    else components.relative_time(ended, now_ns)
                ),
                title=None if ended is None else components.absolute_time(ended),
                className="num",
            ),
        ],
        className="lost" if row in lost else None,
    )


def _kvg(pairs: list[tuple[str, str]]) -> html.Div:
    children: list[Component | str] = []
    for key, value in pairs:
        children.append(html.Span(key, className="k"))
        children.append(html.Span(value, className="v"))
    return html.Div(children, className="kvg")


def _lineage_text(
    trial: TrialRecord, data: SweepPageData, numbers: dict[str, int]
) -> str | None:
    root = str(trial.retry_root_trial_id)
    chain = sorted(
        (entry for entry in data.lineage if entry["root"] == root),
        key=lambda entry: entry["index"],
    )
    if len(chain) <= 1:
        return None

    def step(entry: dict[str, Any]) -> str:
        trial_id = str(entry["trial_id"])
        return f"#{numbers.get(trial_id, components.short_id(trial_id))}"

    steps = " → ".join(step(entry) for entry in chain)
    root_mark = f"#{numbers.get(root, components.short_id(root))}"
    return f"{steps} · retry index {trial.retry_index} · root {root_mark}"


def _trial_subrow(
    trial: TrialRecord, data: SweepPageData, index: int, span: int
) -> html.Tr | None:
    numbers = _numbers(data)
    params = [
        (row.key, _param_text(row.value))
        for row in data.params
        if str(row.trial_id) == str(trial.trial_id)
    ]
    catalog = [
        (
            row.key,
            f"{row.kind} · {row.n_points} pts · step {row.latest_step}"
            f" · {row.n_trials} trials",
        )
        for row in data.catalogs.get(str(trial.trial_id), ())
    ]
    chain = _lineage_text(trial, data, numbers)
    if not (params or catalog or chain):
        return None
    inner: list[Component | str] = []
    if params:
        inner.append(_kvg(params))
    if catalog:
        inner.append(_kvg(catalog))
    if chain:
        inner.append(html.Div(chain, className="annotate"))
    return html.Tr(
        html.Td(inner, colSpan=span),
        className="params-subrow",
        hidden=True,
        id={"trial-subrow": index},
    )


def _trials_section(data: SweepPageData, picked_families: set[str]) -> html.Section:
    numbers = _numbers(data)
    lost = _lost_executions(data)
    lost_numbers = {numbers.get(str(row.trial_id), -1) for row in lost}
    failed_ids = _failed_trial_ids(data)
    has_retries = any(row.retry_index not in (None, 0) for row in data.trials)
    head: list[Component | str] = [
        html.Th(className="selbox"),
        head_cell("Trial"),
        head_cell("State"),
        head_cell("Objective", numeric=True),
    ]
    span = 5
    if has_retries:
        head.append(head_cell("Lineage"))
        span = 6
    head.append(head_cell("Artifacts"))
    rows: list[Component | str] = []
    for index, trial in enumerate(data.trials):
        root = str(trial.retry_root_trial_id)
        chips = [
            (row.key, str(row.artifact_id), row.filename)
            for row in data.artifacts
            if str(row.trial_id) == str(trial.trial_id)
        ]
        cells: list[Component | str] = [
            html.Td(
                dcc.Checklist(
                    id={"sweep-trial-pick": index},
                    options=[{"label": "", "value": root}],
                    value=[root] if root in picked_families else [],
                ),
                className="selbox",
            ),
            html.Td(
                html.Span(
                    [
                        f"#{trial.number} ",
                        html.Span("▸", className="chev", id={"trial-chev": index}),
                    ],
                    id={"trial-toggle": index},
                    className="trial-toggle",
                ),
                className="num",
            ),
            html.Td(_trial_state_dot(trial, lost_numbers, failed_ids)),
            html.Td(_sig(trial.objective), className="num"),
        ]
        if has_retries:
            cells.append(
                html.Td(
                    "root"
                    if trial.retry_index in (None, 0)
                    else f"retry {trial.retry_index}",
                    className="annotate",
                )
            )
        cells.append(html.Td(artifact_chips(chips), className="art-cell"))
        rows.append(html.Tr(cells, className="trial-row"))
        subrow = _trial_subrow(trial, data, index, span)
        if subrow is not None:
            rows.append(subrow)
    return html.Section([html.H2("Trials"), scroll_table(head, rows)])


def _params_table(data: SweepPageData) -> html.Section:
    aggregate: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in data.params:
        if row.key not in aggregate:
            aggregate[row.key] = {"kind": row.kind, "values": []}
            order.append(row.key)
        text = _param_text(row.value)
        if text not in aggregate[row.key]["values"]:
            aggregate[row.key]["values"].append(text)

    def cell(values: list[str]) -> str:
        if len(values) == 1:
            return values[0]
        if len(values) <= 6:
            return ", ".join(sorted(values))
        try:
            numbers = [float(value) for value in values]
        except ValueError:
            return f"{len(values)} distinct values"
        return f"{_sig(min(numbers))}–{_sig(max(numbers))} ({len(values)} values)"

    rows = [
        html.Tr(
            [
                html.Td(aggregate[key]["kind"], className="klabel"),
                html.Td(key, className="klabel"),
                html.Td(cell(aggregate[key]["values"]), className="pval"),
            ]
        )
        for key in order
    ]
    return html.Section(
        [
            html.H2("Params"),
            html.Div(
                html.Table(
                    [
                        html.Thead(
                            html.Tr(
                                [
                                    head_cell("Kind"),
                                    head_cell("Key"),
                                    head_cell("Value"),
                                ]
                            )
                        ),
                        html.Tbody(rows),
                    ]
                ),
                className="section ptable",
            ),
        ]
    )


def render(
    data: SweepPageData,
    project: str,
    sweep_id: str,
    now_ns: int,
    picked_families: set[str],
    view: str | None = None,
) -> list[Component | str]:
    """The shared chrome: crumb, heading, sub-nav, actions, the Python
    disclosure, provenance, and — on the overview — the Executions,
    Trials, and Params tables."""
    numbers = _numbers(data)
    lost = _lost_executions(data)
    ordered = sorted(
        data.executions,
        key=lambda row: (
            numbers.get(str(row.trial_id), -1),
            components.datetime_to_ns(row.started_at),
        ),
    )
    return [
        _breadcrumbs(data, project),
        _heading(data),
        _sub_line(data, now_ns),
        _views_row(project, sweep_id, data, view),
        _actions_row(data, project, sweep_id),
        sweep_views.python_disclosure(project, sweep_id),
        _provenance_grid(data, now_ns),
        *(
            []
            if view
            else [
                html.Section(
                    [
                        html.H2("Executions"),
                        scroll_table(
                            [
                                head_cell("Trial", numeric=True),
                                head_cell("Execution"),
                                head_cell("Host"),
                                head_cell("Outcome"),
                                head_cell("Started", numeric=True),
                                head_cell("Ended", numeric=True),
                            ],
                            [
                                _execution_cells(row, numbers, lost, now_ns)
                                for row in ordered
                            ],
                        ),
                    ]
                ),
                _trials_section(data, picked_families),
                _params_table(data),
            ]
        ),
    ]


def _subview(
    service: DashboardService, project: str, sweep_id: str, view: str, now_ns: int
) -> list[Component | str]:
    """The active sub-view's body; every caller has already checked
    :func:`_supported`."""
    if view == "series":
        return [sweep_views.series_body(service, project, sweep_id, now_ns)]
    if view == "points":
        return [sweep_views.points_body(service, project, sweep_id)]
    if view == "search":
        return [sweep_views.search_body(service, project, sweep_id, now_ns)]
    if view == "optuna":
        return [sweep_views.optuna_body(service, project, sweep_id, now_ns)]
    return []


def _supported(view: str | None, data: SweepPageData) -> bool:
    """Whether the sweep's real data supports a sub-view — the same
    gates the sub-nav applies to its links."""
    if view == "series":
        return data.series_supported
    if view == "points":
        return data.overview.started > 0
    if view == "search":
        return bool(data.trials)
    if view == "optuna":
        return any(row.distributions for row in data.trials)
    return False


def page_body(
    service: DashboardService,
    data: SweepPageData,
    project: str,
    sweep_id: str,
    now_ns: int,
    picked_families: set[str],
    view: str | None = None,
) -> list[Component | str]:
    """The full page body: the shared chrome plus the active sub-view;
    an unsupported or unknown view shows the overview."""
    active = view if _supported(view, data) else None
    body = list(render(data, project, sweep_id, now_ns, picked_families, active))
    if active is not None:
        body.extend(_subview(service, project, sweep_id, active, now_ns))
    return body


def page(
    service: DashboardService,
    project: str,
    sweep_id: str,
    via: str | None,
    now_ns: int,
    picked_families: set[str],
    view: str | None = None,
) -> html.Div | None:
    """The full sweep page, or ``None`` when the id names no sweep."""
    data = collect(service, sweep_id, via)
    if data is None:
        return None
    return page_shell(
        "",
        project,
        html.Div(
            page_body(service, data, project, sweep_id, now_ns, picked_families, view),
            id="sweep-page-body",
        ),
        dcc.Store(id="sweep-page-facts-store", data={"digest": digest(data)}),
        wide=True,
        scope="Sweep",
    )
