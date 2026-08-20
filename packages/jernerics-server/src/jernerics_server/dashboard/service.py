"""Thin read-only composition over QueryService for Dash callbacks.

DashboardService contains no SQL: every method delegates to the same
QueryService the HTTP API uses, so the dashboard can never drift from
the one SQL layer. Selections arrive from client state as plain strings
and are rebuilt into typed ``Selection`` objects here, per query call.

View-shaped aggregates (project rollups, per-sweep summaries, family
rows) are internal presentation shapes — plain frozen dataclasses here,
never schema-package wire models.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from jernerics_schema import (
    ExecutionRecord,
    ProvenanceRecord,
    Selection,
    TrialParamRecord,
    ValueCatalogRecord,
)

from jernerics_server.queries import QueryService


@dataclass(frozen=True)
class ProjectSummary:
    """One project row on the catalog page."""

    project: str
    active: int
    quiet: int
    stale: int
    unknown: int
    succeeded: int
    failed: int
    recent_sweep: str | None
    last_activity_ns: int | None


@dataclass(frozen=True)
class SweepSummary:
    """One sweep row in the workspace grid (and the sweep page header)."""

    sweep_id: str
    name: str
    state: str
    backend: str | None
    submitted_jobs: int
    expected_trials: int | None
    started: int
    terminal: int
    active: int
    quiet: int
    stale: int
    unknown: int
    succeeded: int
    failed: int
    latest_submitted_ns: int | None
    waiting_trials: int
    running_trials: int

    @property
    def health(self) -> str:
        """Failing beats attention beats healthy; derived, never stored."""
        if self.failed:
            return "failing"
        if self.stale:
            return "attention"
        return "healthy"

    @property
    def incomplete(self) -> bool:
        """True while trials wait/run or executions are non-terminal."""
        return bool(
            self.waiting_trials or self.running_trials or self.started > self.terminal
        )


@dataclass(frozen=True)
class FamilyRow:
    """One retry family (root trial) in the sweep page grid."""

    root: str
    current_trial: str
    number: int
    state: str
    objective: float | None
    generations: int
    params: tuple[tuple[str, str], ...] = ()

    @property
    def retry_count(self) -> int:
        return self.generations - 1


@dataclass(frozen=True)
class SweepDetail:
    """Everything the sweep page renders, fetched in batched queries."""

    context: dict[str, Any]
    overview: SweepSummary
    jobs: list[dict[str, Any]]
    progress: list[dict[str, Any]]
    families: list[FamilyRow]
    lineage: list[dict[str, Any]]


@dataclass(frozen=True)
class TrialDetail:
    """Everything the trial page renders."""

    context: dict[str, Any]
    params: list[TrialParamRecord]
    catalog: list[ValueCatalogRecord]
    executions: list[ExecutionRecord]
    lineage: list[dict[str, Any]]


@dataclass(frozen=True)
class ExecutionDetail:
    """Everything the execution page renders."""

    context: dict[str, Any]
    params: list[TrialParamRecord]
    provenance: list[ProvenanceRecord]
    resolved_config: dict[str, Any] | None


def _format_param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


@dataclass(frozen=True)
class DashboardService:
    """The only data doorway callbacks are allowed to use."""

    queries: QueryService

    def projects(self) -> list[str]:
        return self.queries.projects()

    def selection(self, project: str | None, sweep_ids: Sequence[str]) -> Selection:
        """Typed Selection for the current project plus tray sweep ids."""
        if not project:
            raise ValueError("no project selected")
        return Selection(
            project=project,
            sweeps=tuple(uuid.UUID(sweep_id) for sweep_id in sweep_ids),
        )

    def project_catalog(self) -> list[ProjectSummary]:
        return [
            ProjectSummary(
                project=row["project"],
                active=row["active"],
                quiet=row["quiet"],
                stale=row["stale"],
                unknown=row["unknown"],
                succeeded=row["succeeded"],
                failed=row["failed"],
                recent_sweep=row["recent_sweep"],
                last_activity_ns=row["last_activity_ns"],
            )
            for row in self.queries.project_catalog()
        ]

    def sweep_overview(
        self, project: str, sweep_ids: Sequence[str] = ()
    ) -> list[SweepSummary]:
        selection = Selection(
            project=project,
            sweeps=tuple(uuid.UUID(s) for s in sweep_ids) or None,
        )
        return [
            SweepSummary(
                sweep_id=row["sweep_id"],
                name=row["name"],
                state=row["state"],
                backend=row["backend"],
                submitted_jobs=row["submitted_jobs"],
                expected_trials=row["expected_trials"],
                started=row["started"],
                terminal=row["terminal"],
                active=row["active"],
                quiet=row["quiet"],
                stale=row["stale"],
                unknown=row["unknown"],
                succeeded=row["succeeded"],
                failed=row["failed"],
                latest_submitted_ns=row["latest_submitted_ns"],
                waiting_trials=row["waiting_trials"],
                running_trials=row["running_trials"],
            )
            for row in self.queries.sweep_overview(selection)
        ]

    def sweep_detail(self, sweep_id: str) -> SweepDetail | None:
        """Sweep page data; ``None`` when the id matches no sweep."""
        parsed = _parse_id(sweep_id)
        if parsed is None:
            return None
        context = self.queries.sweep_context(parsed)
        if context is None:
            return None
        selection = Selection(project=context["project"], sweeps=(parsed,))
        overview_rows = self.sweep_overview(context["project"], [sweep_id])
        if not overview_rows:
            return None
        families = self.queries.trial_families(selection)
        current_ids = [uuid.UUID(row["current_trial"]) for row in families]
        params, _ = (
            self.queries.trial_params(
                Selection(
                    project=context["project"],
                    sweeps=(parsed,),
                    trials=tuple(current_ids),
                )
            )
            if current_ids
            else ([], None)
        )
        grouped: dict[str, list[TrialParamRecord]] = {}
        for record in params:
            grouped.setdefault(str(record.trial_id), []).append(record)
        family_rows = [
            FamilyRow(
                root=row["root"],
                current_trial=row["current_trial"],
                number=row["number"],
                state=row["state"],
                objective=row["objective"],
                generations=row["generations"],
                params=tuple(
                    (record.key, _format_param(record.value))
                    for record in sorted(
                        grouped.get(row["current_trial"], ()),
                        key=lambda record: record.key,
                    )
                ),
            )
            for row in families
        ]
        return SweepDetail(
            context=context,
            overview=overview_rows[0],
            jobs=self.queries.submission_jobs(selection),
            progress=[
                row
                for row in self.queries.execution_progress(selection)
                if row["ended_ns"] is None
            ],
            families=family_rows,
            lineage=[
                {
                    "trial_id": str(record.trial_id),
                    "parent": (
                        str(record.retry_of_trial_id)
                        if record.retry_of_trial_id
                        else ""
                    ),
                    "root": str(record.retry_root_trial_id),
                    "index": record.retry_index,
                }
                for record in self.queries.lineage(selection)
            ],
        )

    def trial_detail(self, trial_id: str) -> TrialDetail | None:
        """Trial page data; ``None`` when the id matches no trial."""
        parsed = _parse_id(trial_id)
        if parsed is None:
            return None
        context = self.queries.trial_context(parsed)
        if context is None:
            return None
        project = context["project"]
        root = uuid.UUID(context["retry_root_trial_id"])
        family = Selection(project=project, retry_roots=(root,))
        named = Selection(project=project, trials=(parsed,))
        return TrialDetail(
            context=context,
            params=self.queries.trial_params(named)[0],
            catalog=self.queries.value_catalog(family),
            executions=self.queries.executions(family),
            lineage=[
                {
                    "trial_id": str(record.trial_id),
                    "parent": (
                        str(record.retry_of_trial_id)
                        if record.retry_of_trial_id
                        else ""
                    ),
                    "root": str(record.retry_root_trial_id),
                    "index": record.retry_index,
                }
                for record in self.queries.lineage(family)
            ],
        )

    def execution_detail(self, execution_id: str) -> ExecutionDetail | None:
        """Execution page data; ``None`` when the id matches no execution."""
        parsed = _parse_id(execution_id)
        if parsed is None:
            return None
        context = self.queries.execution_context(parsed)
        if context is None:
            return None
        project = context["project"]
        trial = uuid.UUID(context["trial_id"])
        selection = Selection(project=project, trials=(trial,))
        sweep_selection = Selection(
            project=project, sweeps=(uuid.UUID(context["sweep_id"]),)
        )
        resolved = None
        for record in self.queries.values(selection, keys=("resolved_config",))[0]:
            resolved = record.observation
        return ExecutionDetail(
            context=context,
            params=self.queries.trial_params(selection)[0],
            provenance=self.queries.provenance(sweep_selection),
            resolved_config=resolved,
        )
