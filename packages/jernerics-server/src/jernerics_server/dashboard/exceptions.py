import time
from urllib.parse import parse_qs, quote

from dash import dcc, html
from dash.development.base_component import Component

from . import components, page
from .components import MISSING, UNKNOWN
from .routes import ROUTES_BASE
from .service import DashboardService, FailedExecutionRow, SweepSummary

MODES = ("cause", "sweep", "host")

_MODE_LABELS = {"cause": "By cause", "sweep": "By sweep", "host": "By host"}

_FAILURE_LIMIT = 200


def href(project: str, *, scope_all: bool = False) -> str:
    quoted = quote(project, safe="")
    target = f"{ROUTES_BASE}/project/{quoted}/exceptions"
    return f"{target}?scope=all" if scope_all else target


def scope_all(search: str | None) -> bool:
    return _query(search).get("scope") == ["all"]


def focus_sweep(search: str | None) -> str | None:
    values = _query(search).get("sweep")
    return values[0] if values else None


def _query(search: str | None) -> dict[str, list[str]]:
    return parse_qs((search or "").lstrip("?"))


def exceptions_page(
    service: DashboardService,
    project: str,
    *,
    search: str | None = None,
    now_ns: int | None = None,
) -> html.Div:
    now = time.time_ns() if now_ns is None else now_ns
    all_scope = scope_all(search)
    summaries = service.sweep_overview(project)
    fails = service.failed_executions(
        project, limit=_FAILURE_LIMIT, include_curated=all_scope
    )
    curated = sum(1 for summary in summaries if summary.archived or summary.invalid)
    body = [
        html.H1("Exceptions"),
        html.P(
            f"{len(fails)} failed executions · "
            + (
                "All sweeps — historical"
                if all_scope
                else "Active sweeps — not yet curated"
            ),
            className="sub",
        ),
        page.limit_row(
            page.segment(
                [
                    ("Active (to triage)", href(project), not all_scope),
                    ("All (historical)", href(project, scope_all=True), all_scope),
                ]
            ),
            *(
                [
                    html.Span(
                        f"active excludes {curated} curated sweeps",
                        className="annotate",
                    )
                ]
                if curated
                else []
            ),
            *(
                [
                    html.P(
                        f"Showing the most recent {_FAILURE_LIMIT}.",
                        className="annotate",
                    )
                ]
                if len(fails) >= _FAILURE_LIMIT
                else []
            ),
        ),
        _bulkbar(),
        html.Div(id="exc-note"),
        html.Div(
            _groupsets(
                fails,
                summaries,
                now_ns=now,
                visible_mode="cause",
                focus=focus_sweep(search),
            ),
            id="exc-groupsets",
        ),
        dcc.Store(id="exc-selection-store"),
    ]
    return page.page_shell("Exceptions", project, *body)


def rollup(
    service: DashboardService,
    project: str,
    *,
    scope_all: bool,
    now_ns: int,
    visible_mode: str = "cause",
) -> list[html.Div]:
    """The three grouping sets, re-read after a triage action."""
    return _groupsets(
        service.failed_executions(
            project, limit=_FAILURE_LIMIT, include_curated=scope_all
        ),
        service.sweep_overview(project),
        now_ns=now_ns,
        visible_mode=visible_mode,
    )


def action_note(ok: bool, text: str) -> html.Div:
    return html.Div(text, className=f"action-note {'ok' if ok else 'err'}")


def _groupsets(
    fails: list[FailedExecutionRow],
    summaries: list[SweepSummary],
    *,
    now_ns: int,
    visible_mode: str,
    focus: str | None = None,
) -> list[html.Div]:
    by_id = {summary.sweep_id: summary for summary in summaries}
    return [
        _groupset(
            fails,
            by_id,
            mode,
            visible=mode == visible_mode,
            now_ns=now_ns,
            focus=focus,
        )
        for mode in MODES
    ]


def _bulkbar() -> html.Div:
    return html.Div(
        [
            html.Span(
                "0 sweeps selected",
                id="exc-selection-count",
                className="num",
                style={"fontWeight": 600},
            ),
            dcc.Input(
                id="exc-reason",
                type="text",
                placeholder="Reason (required for Mark invalid)",
            ),
            html.Button("Mark invalid", id="exc-mark-invalid", className="btn-primary"),
            html.Button("Clear", id="exc-clear"),
            html.Span(className="spacer"),
            html.Div(
                [
                    html.Span(
                        label,
                        **{"data-mode": mode},  # ty: ignore[invalid-argument-type]
                        className="gmode on" if mode == "cause" else "gmode",
                    )
                    for mode, label in _MODE_LABELS.items()
                ],
                id="exc-mode-seg",
                className="seg",
            ),
        ],
        className="bulkbar",
    )


def _groupset(
    fails: list[FailedExecutionRow],
    summaries: dict[str, SweepSummary],
    mode: str,
    *,
    visible: bool,
    now_ns: int,
    focus: str | None,
) -> html.Div:
    buckets: dict[tuple, list[FailedExecutionRow]] = {}
    for row in fails:
        if mode == "sweep":
            key: tuple = (row.sweep_id,)
        elif mode == "host":
            key = (row.hostname or UNKNOWN,)
        else:
            key = (
                row.failure_kind or UNKNOWN,
                row.failure_summary or "",
                row.exit_code,
            )
        buckets.setdefault(key, []).append(row)
    groups = [
        _group(key, rows, summaries, mode, now_ns=now_ns, focus=focus)
        for key, rows in sorted(buckets.items(), key=lambda item: -len(item[1]))
    ]
    return html.Div(
        groups,
        className="groupset",
        **{"data-mode": mode},  # ty: ignore[invalid-argument-type]
        hidden=not visible,
    )


def _group(
    key: tuple,
    rows: list[FailedExecutionRow],
    summaries: dict[str, SweepSummary],
    mode: str,
    *,
    now_ns: int,
    focus: str | None,
) -> html.Details:
    sweep_count = len({row.sweep_id for row in rows})
    by_sweep: dict[str, list[FailedExecutionRow]] = {}
    for row in rows:
        by_sweep.setdefault(row.sweep_id, []).append(row)
    inner = [
        _sweep_details(
            sweep_id,
            srows,
            summaries.get(sweep_id),
            mode,
            now_ns=now_ns,
            focused=sweep_id == focus,
        )
        for sweep_id, srows in sorted(by_sweep.items())
    ]
    return html.Details(
        [
            html.Summary(
                [
                    dcc.Input(
                        type="checkbox",  # ty: ignore[invalid-argument-type]
                        className="sel-group",
                    ),
                    *_head(mode, key, rows),
                    html.Span(f"×{len(rows)}", className="count-badge"),
                    html.Span(_plural(sweep_count, "sweep"), className="sweep-count"),
                ]
            ),
            html.Div(inner, style={"padding": "4px 12px 12px"}),
        ],
        className="failgroup",
        open=True,
    )


def _head(
    mode: str, key: tuple, rows: list[FailedExecutionRow]
) -> list[Component | str]:
    if mode == "sweep":
        return [rows[0].sweep_name]
    if mode == "host":
        return [f"host {key[0]}"]
    kind, summary, exit_code = key
    head: list[Component | str] = [
        html.Span(
            f"{kind} · exit code {MISSING if exit_code is None else exit_code}",
            className="crit-text",
        )
    ]
    if summary:
        head.append(f" — {summary}")
    return head


def _sweep_details(
    sweep_id: str,
    rows: list[FailedExecutionRow],
    summary: SweepSummary | None,
    mode: str,
    *,
    now_ns: int,
    focused: bool,
) -> html.Details:
    badges = []
    if summary is not None and summary.invalid:
        badges.append(html.Span("invalid", className="badge invalid"))
    if summary is not None and summary.archived:
        badges.append(html.Span("archived", className="badge archived"))
    signal = _signal(summary, mode)
    return html.Details(
        [
            html.Summary(
                [
                    dcc.Input(
                        type="checkbox",  # ty: ignore[invalid-argument-type]
                        className="sel-sweep",
                        name=sweep_id,
                    ),
                    rows[0].sweep_name,
                    *badges,
                    html.Span(str(len(rows)), className="count-badge neutral"),
                    *(  # one nested span keeps the badge and the line apart
                        [html.Span(signal, className="sweep-count")] if signal else []
                    ),
                ]
            ),
            _executions_table(rows, now_ns),
        ],
        id=f"sweep-{sweep_id}",
        className="failgroup",
        style={"marginLeft": "16px"},
        open=focused,
    )


def _signal(summary: SweepSummary | None, mode: str) -> list[Component | str]:
    """The per-sweep rollup line: empty on the sweep axis when nothing
    is wrong, systematic-vs-isolated elsewhere (prototype failure_groups)."""
    failed = summary.failed if summary else 0
    lost = max((summary.started - summary.terminal) if summary else 0, 0)
    trials = summary.trials if summary else 0
    executions = f"{failed} failed execution{'s' if failed != 1 else ''}"
    trial_word = _plural(trials, "trial")
    if mode == "sweep":
        parts: list[Component | str] = []
        if failed:
            if trials and failed >= trials:
                parts.append(
                    html.Span(
                        f"{'all' if trials != 1 else 'the only'} {trials} "
                        f"{trial_word} failed — systematic",
                        className="crit-text",
                    )
                )
            else:
                parts.append(f"{executions} across {trial_word} — isolated")
        if lost:
            parts.append(
                html.Span(f"{lost} lost — no terminal event", className="warn-text")
            )
        return _join(parts)
    if trials and failed >= trials:
        return [
            html.Span(
                f"every trial affected — systematic ({executions} / {trial_word})",
                className="crit-text",
            )
        ]
    return [f"{executions} across {trial_word} — isolated"]


def _join(parts: list[Component | str]) -> list[Component | str]:
    joined: list[Component | str] = []
    for part in parts:
        if joined:
            joined.append(" · ")
        joined.append(part)
    return joined


def _executions_table(rows: list[FailedExecutionRow], now_ns: int) -> html.Table:
    return html.Table(
        [
            html.Thead(
                html.Tr(
                    [
                        html.Th("Trial", className="num"),
                        html.Th("Execution"),
                        html.Th("Host"),
                        html.Th("Summary"),
                        html.Th("When", className="num"),
                    ]
                )
            ),
            html.Tbody(
                [
                    html.Tr(
                        [
                            html.Td(f"#{row.trial_number}", className="num"),
                            html.Td(
                                components.short_id(row.execution_id), className="mono"
                            ),
                            html.Td(row.hostname or MISSING),
                            html.Td(row.failure_summary or ""),
                            html.Td(
                                components.relative_time(row.updated_ns, now_ns),
                                className="num",
                            ),
                        ]
                    )
                    for row in rows
                ]
            ),
        ]
    )


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"
