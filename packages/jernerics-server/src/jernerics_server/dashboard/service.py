"""Composition over QueryService (reads) and Store (curation writes)
for Dash callbacks.

DashboardService contains no SQL: every read delegates to the same
QueryService the HTTP API uses, and every curation mutation delegates
to the explicit Store methods, so the dashboard can never drift from
the one SQL layer. Selections arrive from client state as plain strings
and are rebuilt into typed ``Selection`` objects here, per query call.

View-shaped aggregates (project rollups, per-sweep summaries, family
rows) are internal presentation shapes — plain frozen dataclasses here,
never schema-package wire models.
"""

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from jernerics_schema import (
    ExecutionRecord,
    Page,
    ProvenanceRecord,
    Selection,
    TrialParamRecord,
    ValueCatalogRecord,
    ValueRecord,
)

from jernerics_server.queries import QueryService
from jernerics_server.store import (
    InvalidCurationReasonError,
    Store,
    StoreError,
    SweepNotFoundError,
    SweepStillInvalidError,
)

ANALYSIS_REDUCTIONS = ("none", "mean", "min", "max")
"""Explicit execution-reduction choices for the series overlay; "none"
shows every (trial, execution) series as logged."""

WORKSPACE_VIEWS = ("current", "archived", "all")
"""Workspace review views over a project's sweeps."""


class CurationUnavailableError(Exception):
    """A curation mutation was requested without an injected Store."""


class CurationRejectedError(Exception):
    """The Store refused a curation mutation; the message is the
    user-visible reason."""


def _curation_error(error: StoreError) -> str:
    if isinstance(error, SweepNotFoundError):
        return "no sweep matches this id"
    if isinstance(error, SweepStillInvalidError):
        return "remains invalid — restore validity before unarchiving"
    if isinstance(error, InvalidCurationReasonError):
        return "reason must be 1..500 characters after trimming"
    return str(error)


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
    archived_sweeps: int
    invalid_sweeps: int


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
    archived_ns: int | None
    invalid_ns: int | None
    invalid_reason: str | None

    @property
    def archived(self) -> bool:
        return self.archived_ns is not None

    @property
    def invalid(self) -> bool:
        return self.invalid_ns is not None

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

    @property
    def current(self) -> bool:
        """True while incomplete regardless of curation, or terminal and
        neither archived nor invalid."""
        return self.incomplete or not (self.archived or self.invalid)


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
    executions: list[ExecutionRecord]
    families: list[FamilyRow]
    lineage: list[dict[str, Any]]


@dataclass(frozen=True)
class ArtifactRow:
    """One artifact version row: repeated keys number v1..vN by
    declaration time and are never collapsed."""

    artifact_id: str
    key: str
    version: int
    versions: int
    execution_id: str | None
    filename: str
    content_type: str
    size_bytes: int
    sha256: str | None
    context: dict[str, Any] | None
    source: str
    declared_ns: int
    received_ns: int | None

    @property
    def available(self) -> bool:
        return self.received_ns is not None


@dataclass(frozen=True)
class ArtifactView:
    """Everything the artifact viewer page renders."""

    artifact_id: str
    key: str
    version: int
    versions: int
    execution_id: str | None
    filename: str
    content_type: str
    size_bytes: int
    sha256: str | None
    context: dict[str, Any] | None
    source: str
    declared_ns: int
    received_ns: int | None
    trial_id: str
    sweep_id: str
    sweep_name: str
    project: str

    @property
    def available(self) -> bool:
        return self.received_ns is not None


@dataclass(frozen=True)
class TrialDetail:
    """Everything the trial page renders."""

    context: dict[str, Any]
    params: list[TrialParamRecord]
    catalog: list[ValueCatalogRecord]
    executions: list[ExecutionRecord]
    lineage: list[dict[str, Any]]
    artifacts: tuple["ArtifactRow", ...] = ()


@dataclass(frozen=True)
class ExecutionDetail:
    """Everything the execution page renders."""

    context: dict[str, Any]
    params: list[TrialParamRecord]
    provenance: list[ProvenanceRecord]
    resolved_config: dict[str, Any] | None
    artifacts: tuple["ArtifactRow", ...] = ()


def _format_param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def workspace_visible(
    summaries: Sequence[SweepSummary], view: str
) -> list[SweepSummary]:
    """The sweeps one workspace view shows.

    ``current`` keeps every incomplete sweep (curation never hides
    active work) plus terminal unarchived/valid ones; ``archived``
    shows terminal archived sweeps, invalid sweeps included; ``all``
    shows everything. Unknown views fall back to ``current``.
    """
    if view == "all":
        return list(summaries)
    if view == "archived":
        return [s for s in summaries if s.archived and not s.incomplete]
    return [s for s in summaries if s.current]


def view_counts(summaries: Sequence[SweepSummary]) -> dict[str, int]:
    """Row counts the workspace view controls carry."""
    return {
        "current": sum(1 for s in summaries if s.current),
        "archived": sum(1 for s in summaries if s.archived and not s.incomplete),
        "all": len(summaries),
    }


@dataclass(frozen=True)
class DashboardService:
    """The only data doorway callbacks are allowed to use."""

    queries: QueryService
    store: Store | None = None

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
                archived_sweeps=row["archived_sweeps"],
                invalid_sweeps=row["invalid_sweeps"],
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
        return self._sweep_summaries(selection)

    def _sweep_summaries(self, selection: Selection) -> list[SweepSummary]:
        """SweepSummary rows for a fully-formed selection."""
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
                archived_ns=row["archived_ns"],
                invalid_ns=row["invalid_ns"],
                invalid_reason=row["invalid_reason"],
            )
            for row in self.queries.sweep_overview(selection)
        ]

    def _curation_store(self) -> Store:
        if self.store is None:
            raise CurationUnavailableError(
                "curation is unavailable: this dashboard has no write store"
            )
        return self.store

    def sweep_label(self, sweep_id: str) -> str:
        """Display label (sweep name, else the short id) for reports."""
        parsed = _parse_id(sweep_id)
        context = self.queries.sweep_context(parsed) if parsed else None
        if context:
            return str(context["name"])
        return sweep_id.replace("-", "")[:8]

    def archive_sweep(self, sweep_id: str) -> str:
        """Archive a sweep; returns its label for the action report."""
        try:
            self._curation_store().archive_sweep(sweep_id)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error
        return self.sweep_label(sweep_id)

    def restore_sweep(self, sweep_id: str) -> str:
        """Restore an archived sweep; returns its label on success."""
        try:
            self._curation_store().restore_sweep(sweep_id)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error
        return self.sweep_label(sweep_id)

    def mark_sweep_invalid(self, sweep_id: str, reason: str) -> str:
        """Mark a sweep scientifically invalid; returns its label."""
        try:
            self._curation_store().mark_sweep_invalid(sweep_id, reason)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error
        return self.sweep_label(sweep_id)

    def restore_sweep_validity(self, sweep_id: str) -> str:
        """Clear the invalid facts; returns the sweep's label."""
        try:
            self._curation_store().restore_sweep_validity(sweep_id)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error
        return self.sweep_label(sweep_id)

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
            executions=self.sweep_executions(selection),
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

    def sweep_executions(self, selection: Selection) -> list[ExecutionRecord]:
        """Every execution under the selection's sweeps, with derived
        monitoring, for the sweep page's execution list."""
        return self.queries.executions(selection)

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
            artifacts=self.trial_artifacts(context["trial_id"]),
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
            artifacts=self.execution_artifacts(execution_id),
        )

    # -- Artifacts (jernerics-h5d.14) ------------------------------------

    @staticmethod
    def _artifact_rows(records: Sequence[dict[str, Any]]) -> tuple[ArtifactRow, ...]:
        """Version rows: records arrive key/declaration ordered, so a
        running per-key counter numbers v1..vN without collapsing."""
        totals: dict[str, int] = {}
        for record in records:
            totals[record["key"]] = totals.get(record["key"], 0) + 1
        seen: dict[str, int] = {}
        rows: list[ArtifactRow] = []
        for record in records:
            seen[record["key"]] = seen.get(record["key"], 0) + 1
            rows.append(
                ArtifactRow(
                    artifact_id=record["artifact_id"],
                    key=record["key"],
                    version=seen[record["key"]],
                    versions=totals[record["key"]],
                    execution_id=record["execution_id"],
                    filename=record["filename"],
                    content_type=record["content_type"],
                    size_bytes=record["size_bytes"],
                    sha256=record["sha256"],
                    context=record["context"],
                    source=record["source"],
                    declared_ns=record["declared_ns"],
                    received_ns=record["received_ns"],
                )
            )
        return tuple(rows)

    def trial_artifacts(self, trial_id: str) -> tuple[ArtifactRow, ...]:
        """All artifact versions of one trial (``()`` for unknown ids)."""
        parsed = _parse_id(trial_id)
        if parsed is None:
            return ()
        return self._artifact_rows(self.queries.trial_artifacts(parsed))

    def execution_artifacts(self, execution_id: str) -> tuple[ArtifactRow, ...]:
        """Execution-bound artifact rows. Versions stay numbered within
        the whole trial so the grid and the viewer page always agree."""
        parsed = _parse_id(execution_id)
        if parsed is None:
            return ()
        context = self.queries.execution_context(parsed)
        if context is None:
            return ()
        return tuple(
            row
            for row in self.trial_artifacts(context["trial_id"])
            if row.execution_id == str(parsed)
        )

    def artifact_view(self, artifact_id: str) -> ArtifactView | None:
        """Viewer page data; ``None`` when the id matches no artifact."""
        parsed = _parse_id(artifact_id)
        if parsed is None:
            return None
        context = self.queries.artifact_context(parsed)
        if context is None:
            return None
        row = next(
            (
                row
                for row in self.trial_artifacts(context["trial_id"])
                if row.artifact_id == str(parsed)
            ),
            None,
        )
        if row is None:
            return None
        return ArtifactView(
            artifact_id=row.artifact_id,
            key=row.key,
            version=row.version,
            versions=row.versions,
            execution_id=row.execution_id,
            filename=row.filename,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            sha256=row.sha256,
            context=row.context,
            source=row.source,
            declared_ns=row.declared_ns,
            received_ns=row.received_ns,
            trial_id=context["trial_id"],
            sweep_id=context["sweep_id"],
            sweep_name=context["sweep_name"],
            project=context["project"],
        )

    def read_artifact_text(self, artifact_id: str, cap: int) -> tuple[str, bool] | None:
        """First ``cap`` bytes of the stored blob as text plus a truncated
        flag; ``None`` when the blob is absent (never raises on decode)."""
        parsed = _parse_id(artifact_id)
        if parsed is None:
            return None
        path = self.queries.artifact_blob_path(parsed)
        if path is None:
            return None
        with open(path, "rb") as blob:
            raw = blob.read(cap)
            truncated = blob.read(1) != b""
        return raw.decode("utf-8", errors="replace"), truncated

    # -- Analysis page (jernerics-h5d.13) ---------------------------------

    _PAGE_FOLLOW_LIMIT = 100
    """Pages followed per analysis read before giving up (100k records)."""

    def _follow_pages(
        self,
        fetch: Callable[..., tuple[list[Any], str | None]],
        selection: Selection,
    ) -> list[Any]:
        """Follow keyset pages of a paginated query to exhaustion."""
        page = Page(limit=1000)
        token: str | None = None
        records: list[Any] = []
        for _ in range(self._PAGE_FOLLOW_LIMIT):
            batch, token = fetch(selection, page, token)
            records.extend(batch)
            if token is None:
                return records
        raise ValueError("analysis read exceeded the pagination follow limit")

    def _follow_values(
        self, selection: Selection, keys: tuple[str, ...] | None
    ) -> list[ValueRecord]:
        return self._follow_pages(
            lambda sel, page, token: self.queries.values(
                sel, keys=keys, page=page, page_token=token
            ),
            selection,
        )

    def _follow_params(self, selection: Selection) -> list[TrialParamRecord]:
        return self._follow_pages(
            lambda sel, page, token: self.queries.trial_params(
                sel, page=page, page_token=token
            ),
            selection,
        )

    def _sweep_names(self, project: str) -> dict[str, str]:
        records = self._follow_pages(
            lambda sel, page, token: self.queries.sweeps(
                sel, page=page, page_token=token
            ),
            Selection(project=project),
        )
        return {str(record.sweep_id): record.name for record in records}

    def analysis_selection(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> Selection:
        """Typed selection for the analysis tray.

        The tray holds sweep ids, explicit trial ids, retry-family roots
        picked in the family grid, and the family-expansion toggle.
        Expansion is resolved client-of-SQL here: with the toggle on,
        roots expand to every generation via :meth:`QueryService.lineage`;
        with it off, a picked family contributes only its current trial.
        """
        tray = tray or {}
        if not project:
            raise ValueError("no project selected")
        trials = [uuid.UUID(value) for value in tray.get("trials") or ()]
        families = [uuid.UUID(value) for value in tray.get("families") or ()]
        if families:
            family = Selection(project=project, retry_roots=tuple(families))
            if tray.get("expand"):
                trials.extend(
                    record.trial_id for record in self.queries.lineage(family)
                )
            else:
                trials.extend(
                    uuid.UUID(row["current_trial"])
                    for row in self.queries.trial_families(family)
                )
        return Selection(
            project=project,
            sweeps=tuple(uuid.UUID(v) for v in tray.get("sweeps") or ()) or None,
            trials=tuple(trials) or None,
            executions=tuple(uuid.UUID(v) for v in tray.get("executions") or ())
            or None,
        )

    def analysis_families(
        self, project: str | None, sweep_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Family-picker rows (one per retry root) for the given sweeps."""
        if not project or not sweep_ids:
            return []
        selection = Selection(
            project=project, sweeps=tuple(uuid.UUID(s) for s in sweep_ids)
        )
        return self.queries.trial_families(selection)

    def analysis_value_keys(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Value keys under the selection: kind, volume, trial and
        retry-family coverage, and the step extent."""
        if not project:
            return []
        selection = self.analysis_selection(project, tray)
        return [
            {
                "key": row["key"],
                "kind": row["kind"],
                "points": row["points"],
                "trials": row["trials"],
                "families": row["families"],
                "steps": (row["max_step"] or 0) > 0,
                "extent": (row["min_step"], row["max_step"]),
            }
            for row in self.queries.value_key_coverage(selection)
        ]

    def analysis_context_dims(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if not project:
            return []
        return self.queries.context_catalog(self.analysis_selection(project, tray))

    def analysis_context_values(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Every distinct flat-context value per dimension under the
        scope, from one values read — the multi-value filter options,
        discovered from stored contexts with no key special-cased."""
        if not project:
            return []
        dims: dict[str, set[str]] = {}
        for record in self._follow_values(self.analysis_selection(project, tray), None):
            for key, value in (record.context.root if record.context else {}).items():
                dims.setdefault(key, set()).add(_format_param(value))
        return [
            {"key": key, "values": sorted(values)}
            for key, values in sorted(dims.items())
        ]

    def analysis_scope_incomplete(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> bool:
        """True while any sweep under the tray's effective selection has
        waiting/running trials or non-terminal executions."""
        if not project:
            return False
        return any(
            summary.incomplete
            for summary in self._sweep_summaries(self.analysis_selection(project, tray))
        )

    def analysis_param_coverage(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Param key x sweep matrix: where each param exists, missing
        cells marked ``None`` (never silently dropped)."""
        if not project:
            return {"sweeps": [], "names": {}, "rows": []}
        selection = self.analysis_selection(project, tray)
        names = self._sweep_names(project)
        sweep_of = {
            str(record.trial_id): str(record.sweep_id)
            for record in self.queries.lineage(selection)
        }
        coverage: dict[str, dict[str, dict[str, Any]]] = {}
        for record in self._follow_params(selection):
            sweep = sweep_of.get(str(record.trial_id))
            if sweep is None:
                continue
            cell = coverage.setdefault(record.key, {}).setdefault(
                sweep, {"trials": 0, "kinds": set()}
            )
            cell["trials"] += 1
            cell["kinds"].add(record.kind)
        sweep_ids = sorted(
            set(sweep_of.values()) | {str(value) for value in selection.sweeps or ()}
        )
        return {
            "sweeps": sweep_ids,
            "names": names,
            "rows": [
                {
                    "key": key,
                    "cells": {
                        sweep: (
                            {
                                "trials": cell["trials"],
                                "kinds": ",".join(sorted(cell["kinds"])),
                            }
                            if (cell := per_sweep.get(sweep)) is not None
                            else None
                        )
                        for sweep in sweep_ids
                    },
                }
                for key, per_sweep in sorted(coverage.items())
            ],
        }

    def analysis_artifacts(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        if not project:
            return []
        records = self._follow_pages(
            lambda sel, page, token: self.queries.artifacts(
                sel, page=page, page_token=token
            ),
            self.analysis_selection(project, tray),
        )
        grouped: dict[str, dict[str, Any]] = {}
        for record in records:
            entry = grouped.setdefault(
                record.key, {"key": record.key, "count": 0, "sources": set()}
            )
            entry["count"] += 1
            entry["sources"].add(record.source)
        return [
            {**entry, "sources": sorted(entry["sources"])}
            for entry in sorted(grouped.values(), key=lambda e: e["key"])
        ]

    def analysis_series(
        self,
        project: str | None,
        tray: dict[str, Any] | None,
        keys: Sequence[str] | None,
        reduction: str = "none",
    ) -> list[dict[str, Any]]:
        """Ordered numeric series for the given value keys from ONE
        paginated values read; a key with no observations maps to an
        empty series list and missing stays missing.

        ``reduction="none"`` returns one series per (trial, execution)
        pair — every logged point, never a silently chosen latest value.
        ``mean``/``min``/``max`` fold executions within each trial, per
        step, independently per key, as an explicit user-chosen
        reduction.
        """
        if reduction not in ANALYSIS_REDUCTIONS:
            raise ValueError(f"unknown reduction {reduction!r}")
        if isinstance(keys, str):
            keys = (keys,)
        wanted = list(dict.fromkeys(key for key in keys or [] if key))
        if not project or not wanted:
            return [{"key": key, "series": []} for key in wanted]
        selection = self.analysis_selection(project, tray)
        grouped: dict[tuple[str, str, str | None], list[tuple[ValueRecord, float]]] = {}
        for record in self._follow_values(selection, tuple(wanted)):
            if not isinstance(record.value, int | float) or isinstance(
                record.value, bool
            ):
                continue
            identity = (
                record.key,
                str(record.trial_id),
                str(record.execution_id) if reduction == "none" else None,
            )
            grouped.setdefault(identity, []).append((record, float(record.value)))
        per_key: dict[str, list[dict[str, Any]]] = {key: [] for key in wanted}
        for (key, trial, execution), group in sorted(grouped.items()):
            by_step: dict[int, list[float]] = {}
            context: dict[str, Any] = {}
            for record, number in group:
                by_step.setdefault(record.step, []).append(number)
                if not context and record.context is not None:
                    context = record.context.root
            if reduction == "mean":
                points = [
                    (step, sum(values) / len(values))
                    for step, values in sorted(by_step.items())
                ]
            elif reduction == "min":
                points = [(step, min(v)) for step, v in sorted(by_step.items())]
            elif reduction == "max":
                points = [(step, max(v)) for step, v in sorted(by_step.items())]
            else:
                points = [(step, values[0]) for step, values in sorted(by_step.items())]
            per_key[key].append(
                {
                    "trial": trial,
                    "execution": execution,
                    "points": points,
                    "context": context,
                }
            )
        return [{"key": key, "series": per_key[key]} for key in wanted]

    def analysis_points(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Grid data for the points tab: non-step value keys (kind and
        per-trial payloads — several executions' points are all kept) and
        flat params per trial for comparison."""
        empty = {
            "trials": [],
            "value_keys": [],
            "values": {},
            "param_keys": [],
            "params": {},
        }
        if not project:
            return empty
        selection = self.analysis_selection(project, tray)
        value_keys = [
            {"key": record.key, "kind": record.kind}
            for record in self.queries.value_catalog(selection)
            if (record.latest_step or 0) == 0
        ]
        trials = [
            {
                "trial_id": str(record.trial_id),
                "sweep_id": str(record.sweep_id),
                "number": record.number,
            }
            for record in sorted(
                self.queries.lineage(selection),
                key=lambda record: (str(record.sweep_id), record.number),
            )
        ]
        values: dict[str, dict[str, list[Any]]] = {}
        if value_keys:
            wanted = tuple(entry["key"] for entry in value_keys)
            for record in self._follow_values(selection, wanted):
                payload = (
                    record.observation
                    if record.observation is not None
                    else record.value
                )
                values.setdefault(str(record.trial_id), {}).setdefault(
                    record.key, []
                ).append(payload)
        params: dict[str, dict[str, Any]] = {}
        for record in self._follow_params(selection):
            params.setdefault(str(record.trial_id), {})[record.key] = record.value
        return {
            "trials": trials,
            "value_keys": value_keys,
            "values": values,
            "param_keys": sorted(
                {key for per_trial in params.values() for key in per_trial}
            ),
            "params": params,
        }

    def analysis_trials(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Optimizer-neutral trial rows (number, state, objective, flat
        params, timestamps) with sweep names attached."""
        if not project:
            return []
        rows = self.queries.trial_numbers_objectives(
            self.analysis_selection(project, tray)
        )
        names = self._sweep_names(project)
        for row in rows:
            row["sweep_name"] = names.get(row["sweep_id"], row["sweep_id"])
        return rows
