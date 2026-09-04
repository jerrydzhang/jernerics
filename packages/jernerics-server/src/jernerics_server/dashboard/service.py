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

import re
import statistics
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jernerics_schema import (
    ArtifactRecord,
    ExecutionRecord,
    Page,
    ProvenanceRecord,
    Selection,
    TrialParamRecord,
    TrialRecord,
    ValueCatalogRecord,
    ValueRecord,
    materialize_selection,
)

from jernerics_server.investigations import (
    InvestigationDetail,
    InvestigationPreview,
    InvestigationRecord,
    InvestigationService,
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
    trials: int
    trials_complete: int
    trials_failed: int
    best_objective: float | None
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
class FailedExecutionRow:
    """One failed execution in the scope-wide failure view."""

    sweep_id: str
    sweep_name: str
    trial_id: str
    trial_number: int
    execution_id: str
    failure_kind: str | None
    failure_summary: str | None
    exit_code: int | None
    hostname: str
    updated_ns: int


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


@dataclass(frozen=True)
class InvestigationRow:
    """One investigation row on the project's Investigations index."""

    investigation_id: str
    name: str
    factor: str
    outcome: str
    member_count: int
    with_outcome: int
    completed: int
    invalid: int
    last_activity_ns: int | None


@dataclass(frozen=True)
class CompareMember:
    """One member sweep of an investigation's Compare view."""

    sweep_id: str
    name: str
    factor_value: str | None
    state: str
    invalid: bool
    archived: bool
    completed: int
    expected_trials: int | None
    usable: int


@dataclass(frozen=True)
class SignatureRow:
    """One sampled signature matched across the analysis set: the
    median outcome each analyzable member produced on it."""

    label: str
    values: dict[str, float | None] = field(default_factory=dict)
    matched: int = 0
    common: bool = False


@dataclass(frozen=True)
class CompareDocument:
    """Everything the Compare view shows, derived from live tracking
    facts: member rows, the analysis set, and the exact-signature
    matches between analyzable members."""

    members: list[CompareMember] = field(default_factory=list)
    signature_keys: tuple[str, ...] = ()
    analyzable: tuple[str, ...] = ()
    excluded_data_bearing: int = 0
    signatures: tuple[SignatureRow, ...] = ()


def _format_param(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _parse_id(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _summary_facts(summary: SweepSummary) -> tuple:
    """Overview row as a digest-stable tuple of stored facts."""
    return (
        str(summary.sweep_id),
        summary.name,
        summary.state,
        summary.backend,
        summary.submitted_jobs,
        summary.expected_trials,
        summary.started,
        summary.terminal,
        summary.active,
        summary.quiet,
        summary.stale,
        summary.unknown,
        summary.succeeded,
        summary.failed,
        summary.latest_submitted_ns,
        summary.waiting_trials,
        summary.running_trials,
        summary.archived_ns,
        summary.invalid_ns,
        summary.invalid_reason,
    )


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
                trials=row["trials"],
                trials_complete=row["trials_complete"],
                trials_failed=row["trials_failed"],
                best_objective=row["best_objective"],
                archived_ns=row["archived_ns"],
                invalid_ns=row["invalid_ns"],
                invalid_reason=row["invalid_reason"],
            )
            for row in self.queries.sweep_overview(selection)
        ]

    def failed_executions(
        self,
        project: str,
        sweep_ids: Sequence[str] = (),
        *,
        limit: int = 200,
        include_curated: bool = False,
    ) -> list[FailedExecutionRow]:
        """Failed executions under the project (optionally narrowed to
        sweeps), curated terminal sweeps excluded most recent first;
        ``include_curated`` pulls the project's historical list."""
        selection = Selection(
            project=project,
            sweeps=tuple(uuid.UUID(s) for s in sweep_ids) or None,
        )
        return [
            FailedExecutionRow(
                sweep_id=row["sweep_id"],
                sweep_name=row["sweep_name"],
                trial_id=row["trial_id"],
                trial_number=row["trial_number"],
                execution_id=row["execution_id"],
                failure_kind=row["failure_kind"],
                failure_summary=row["failure_summary"],
                exit_code=row["exit_code"],
                hostname=row["hostname"],
                updated_ns=row["updated_ns"],
            )
            for row in self.queries.failed_executions(
                selection, limit=limit, include_curated=include_curated
            )
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

    def _investigations(self) -> InvestigationService:
        if self.store is None:
            raise CurationUnavailableError(
                "investigations are unavailable: this dashboard has no write store"
            )
        return InvestigationService(self.store)

    def investigations_index(
        self, project: str, *, include_archived: bool = False
    ) -> list[InvestigationRow]:
        """Index rows for every investigation of the project, with the
        member coverage facts each row shows."""
        service = self._investigations()
        rows: list[InvestigationRow] = []
        for record in service.list_for_project(
            project, include_archived=include_archived
        ):
            coverage = service.detail(str(record.id)).coverage
            rows.append(
                InvestigationRow(
                    investigation_id=str(record.id),
                    name=record.name,
                    factor=record.factor,
                    outcome=record.outcome,
                    member_count=coverage.members,
                    with_outcome=coverage.with_outcome,
                    completed=coverage.completed,
                    invalid=coverage.invalid,
                    last_activity_ns=coverage.last_activity_ns,
                )
            )
        return rows

    def unorganized(self, project: str) -> list[SweepSummary]:
        """The project's sweeps in no investigation (archived
        investigations still organize their members), as overview rows."""
        organized = {
            str(member)
            for record in self._investigations().list_for_project(
                project, include_archived=True
            )
            for member in record.members
        }
        return [
            row
            for row in self.sweep_overview(project)
            if str(row.sweep_id) not in organized
        ]

    def investigation_detail(self, investigation_id: str) -> InvestigationDetail:
        """One investigation with its coverage facts."""
        try:
            return self._investigations().detail(investigation_id)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error

    def create_investigation(
        self,
        project: str,
        name: str,
        factor: str,
        outcome: str,
        *,
        replicate_factor: str | None = None,
        members: Sequence[str] = (),
    ) -> InvestigationRecord:
        """Create an investigation; a conflicting (project, name) is a
        rejection, a matching one returns the stored record."""
        try:
            return self._investigations().create(
                project,
                name,
                factor,
                outcome,
                replicate_factor=replicate_factor,
                members=members,
            )
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error

    def set_investigation_members(
        self, investigation_id: str, sweep_ids: Sequence[str]
    ) -> InvestigationRecord:
        """Replace the member set; returns the updated record."""
        try:
            return self._investigations().set_members(investigation_id, sweep_ids)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error

    def add_investigation_members(
        self, investigation_id: str, sweep_ids: Sequence[str]
    ) -> InvestigationRecord:
        """Add sweeps to the member set; returns the updated record."""
        try:
            return self._investigations().add_members(investigation_id, sweep_ids)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error

    def remove_investigation_members(
        self, investigation_id: str, sweep_ids: Sequence[str]
    ) -> InvestigationRecord:
        """Drop sweeps from the member set; returns the updated record."""
        try:
            return self._investigations().remove_members(investigation_id, sweep_ids)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error

    def archive_investigation(self, investigation_id: str) -> InvestigationRecord:
        """Archive the investigation; returns the updated record."""
        try:
            return self._investigations().archive(investigation_id)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error

    def restore_investigation(self, investigation_id: str) -> InvestigationRecord:
        """Restore the investigation; returns the updated record."""
        try:
            return self._investigations().restore(investigation_id)
        except StoreError as error:
            raise CurationRejectedError(_curation_error(error)) from error

    def investigation_preview(
        self, project: str, sweep_ids: Sequence[str]
    ) -> InvestigationPreview:
        """The shared deterministic member preview (factors, outcomes,
        warnings) — the same contract the HTTP route serves."""
        return self._investigations().preview(project, sweep_ids)

    def investigation_compare(
        self, investigation_id: str, *, include_invalid: bool = False
    ) -> CompareDocument:
        """The Compare view's facts: member rows with derived factor
        values, the analysis set, and every sampled signature matched
        by two or more analyzable members. Invalid members are outside
        the analysis set unless ``include_invalid``; matching is exact
        on the full sampled-parameter signature — no imputation, no
        outlier suppression, and a missing overlap is simply missing."""
        detail = self.investigation_detail(investigation_id)
        record = detail.investigation
        member_ids = [str(sweep) for sweep in record.members]
        selection = materialize_selection(record)
        trials = self.queries.trial_numbers_objectives(selection)
        sweep_of_trial = {str(row["trial_id"]): str(row["sweep_id"]) for row in trials}
        factor_values = self._member_factor_values(
            record.factor, selection, sweep_of_trial
        )
        outcome_of_trial = self._trial_outcomes(selection, record.outcome)
        sampled_keys: set[str] = set()
        signatures_of_trial: dict[str, tuple[tuple[str, str], ...]] = {}
        for row in self._follow_params(selection, kinds=("sampled",)):
            trial = str(row.trial_id)
            signatures_of_trial.setdefault(trial, ())
            signatures_of_trial[trial] = (
                *signatures_of_trial[trial],
                (row.key, _format_param(row.value)),
            )
            sampled_keys.add(row.key)
        signatures_of_trial = {
            trial: tuple(sorted(items)) for trial, items in signatures_of_trial.items()
        }
        members = self._compare_members(
            record,
            member_ids,
            sweep_of_trial,
            outcome_of_trial,
            factor_values,
        )
        analysis_set = [
            member
            for member in members
            if member.usable > 0 and (include_invalid or not member.invalid)
        ]
        by_sweep = {member.sweep_id: member for member in analysis_set}
        matches: dict[tuple[tuple[str, str], ...], dict[str, list[float]]] = {}
        for row in trials:
            if row["state"] != "completed":
                continue
            sweep = str(row["sweep_id"])
            trial = str(row["trial_id"])
            signature = signatures_of_trial.get(trial)
            outcome = outcome_of_trial.get(trial)
            if sweep not in by_sweep or not signature or outcome is None:
                continue
            matches.setdefault(signature, {}).setdefault(sweep, []).append(outcome)
        rows = [
            SignatureRow(
                label=" · ".join(f"{key}={value}" for key, value in signature),
                values={
                    member.sweep_id: (
                        statistics.median(hit)
                        if (hit := sweeps.get(member.sweep_id)) is not None
                        else None
                    )
                    for member in analysis_set
                },
                matched=len(sweeps),
                common=len(sweeps) == len(analysis_set),
            )
            for signature, sweeps in matches.items()
        ]
        rows.sort(key=lambda row: (-row.matched, row.label))
        return CompareDocument(
            members=members,
            signature_keys=tuple(sorted(sampled_keys)),
            analyzable=tuple(member.sweep_id for member in analysis_set),
            excluded_data_bearing=sum(
                1 for member in members if member.invalid and member.usable > 0
            ),
            signatures=tuple(rows),
        )

    def _compare_members(
        self,
        record: InvestigationRecord,
        member_ids: list[str],
        sweep_of_trial: dict[str, str],
        outcome_of_trial: dict[str, float],
        factor_values: dict[str, str | None],
    ) -> list[CompareMember]:
        summaries = {
            summary.sweep_id: summary
            for summary in self.sweep_overview(record.project, member_ids)
        }
        members: list[CompareMember] = []
        for sweep_id in sorted(member_ids, key=lambda sid: (sid not in summaries, sid)):
            summary = summaries.get(sweep_id)
            if summary is None:
                continue
            usable = sum(
                1
                for trial, sweep in sweep_of_trial.items()
                if sweep == sweep_id and trial in outcome_of_trial
            )
            members.append(
                CompareMember(
                    sweep_id=sweep_id,
                    name=summary.name,
                    factor_value=factor_values.get(sweep_id),
                    state=summary.state,
                    invalid=summary.invalid,
                    archived=summary.archived,
                    completed=summary.succeeded,
                    expected_trials=summary.expected_trials,
                    usable=usable,
                )
            )
        members.sort(key=lambda member: member.name.casefold())
        return members

    def _member_factor_values(
        self,
        factor: str,
        selection: Selection,
        sweep_of_trial: dict[str, str],
    ) -> dict[str, str | None]:
        """Each member's value of the comparison factor: a manual param
        named ``factor`` first, then the submission config source, then
        a name token; members carrying none stay missing."""
        names = self._sweep_names(selection.project)
        carried: dict[str, set[str]] = {}
        for row in self._follow_params(selection, kinds=("manual",)):
            if row.key != factor:
                continue
            sweep = sweep_of_trial.get(str(row.trial_id))
            if sweep is not None:
                carried.setdefault(sweep, set()).add(_format_param(row.value))
        for row in self.queries.provenance(selection):
            if row.config_source:
                carried.setdefault(str(row.sweep_id), set()).add(row.config_source)
        for sweep_id, name in names.items():
            for token in re.split(r"[^0-9A-Za-z]+", name):
                if (
                    token
                    and not token.isdigit()
                    and token.casefold() == factor.casefold()
                ):
                    carried.setdefault(sweep_id, set()).add(token)
        return {
            sweep: " / ".join(sorted(values_))
            for sweep, values_ in carried.items()
            if values_
        }

    def _trial_outcomes(self, selection: Selection, outcome: str) -> dict[str, float]:
        """Each trial's outcome value: the median over its executions'
        final-step observations — retried executions contribute their
        own final, nothing is dropped or imputed."""
        finals: dict[str, list[float]] = {}
        per_execution: dict[tuple[str, str], dict[int, list[float]]] = {}
        for row in self._follow_values(selection, (outcome,)):
            execution = str(row.execution_id) if row.execution_id else ""
            steps = per_execution.setdefault((str(row.trial_id), execution), {})
            value = row.value
            if isinstance(value, int | float) and not isinstance(value, bool):
                steps.setdefault(row.step, []).append(float(value))
        for (trial, _execution), steps in per_execution.items():
            last = steps[max(steps)]
            finals.setdefault(trial, []).append(statistics.median(last))
        return {trial: statistics.median(values) for trial, values in finals.items()}

    def sweep_curation_state(self, sweep_id: str) -> SweepSummary | None:
        """One sweep's overview row — archived/invalid facts without the
        full detail; None when the id names no sweep."""
        parsed = _parse_id(sweep_id)
        if parsed is None:
            return None
        context = self.queries.sweep_context(parsed)
        if context is None:
            return None
        rows = self.sweep_overview(str(context["project"]), [sweep_id])
        return rows[0] if rows else None

    def sweep_incomplete(self, sweep_id: str) -> bool:
        """One sweep's liveness from its overview row; the cheap read the
        poll gates use instead of the full detail."""
        summary = self.sweep_curation_state(sweep_id)
        return summary is not None and summary.incomplete

    def sweep_facts(self, sweep_id: str) -> dict[str, Any] | None:
        """Digest-stable cheap facts for one sweep: the overview row plus
        bounded job and in-flight progress identities — no rendered tree,
        nothing wall-clock derived — so the sweep page's tick gate skips
        the full sweep read entirely."""
        parsed = _parse_id(sweep_id)
        if parsed is None:
            return None
        context = self.queries.sweep_context(parsed)
        if context is None:
            return None
        project = str(context["project"])
        rows = self.sweep_overview(project, [sweep_id])
        if not rows:
            return None
        selection = self.selection(project, [sweep_id])
        return {
            "overview": _summary_facts(rows[0]),
            "jobs": [
                (
                    str(job["submission_id"]),
                    str(job["submission_state"] or ""),
                    str(job["job_id"] or ""),
                    str(job["job_state"] or ""),
                )
                for job in self.queries.submission_jobs(selection)
            ],
            "progress": [
                (
                    str(row["execution_id"]),
                    row["current"],
                    row["total"],
                    str(row["unit"] or ""),
                )
                for row in self.queries.execution_progress(selection)
                if row["ended_ns"] is None
            ],
        }

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

    def _sweep_scope(self, sweep_id: str) -> Selection | None:
        """Typed selection over one sweep, or None when no sweep matches."""
        parsed = _parse_id(sweep_id)
        if parsed is None:
            return None
        context = self.queries.sweep_context(parsed)
        if context is None:
            return None
        return Selection(project=context["project"], sweeps=(parsed,))

    def sweep_trials(self, sweep_id: str) -> list[TrialRecord]:
        """Every trial of one sweep with lineage and search-space facts."""
        scope = self._sweep_scope(sweep_id)
        if scope is None:
            return []
        return self._follow_pages(
            lambda sel, page, token: self.queries.trials(
                sel, page=page, page_token=token
            ),
            scope,
        )

    def sweep_trial_params(self, sweep_id: str) -> list[TrialParamRecord]:
        """Every trial's flat params (sampled and manual) under one sweep."""
        scope = self._sweep_scope(sweep_id)
        if scope is None:
            return []
        return self._follow_params(scope)

    def sweep_artifacts(self, sweep_id: str) -> list[ArtifactRecord]:
        """Every artifact declared under one sweep, trial-bound."""
        scope = self._sweep_scope(sweep_id)
        if scope is None:
            return []
        return self._follow_pages(
            lambda sel, page, token: self.queries.artifacts(
                sel, page=page, page_token=token
            ),
            scope,
        )

    def sweep_provenance(self, sweep_id: str) -> list[ProvenanceRecord]:
        """Submission-level provenance rows for one sweep."""
        scope = self._sweep_scope(sweep_id)
        if scope is None:
            return []
        return self.queries.provenance(scope)

    def trial_value_catalogs(
        self, project: str, trial_ids: Sequence[str]
    ) -> dict[str, list[ValueCatalogRecord]]:
        """Per-trial value catalogs; a trial's own executions only."""
        catalogs: dict[str, list[ValueCatalogRecord]] = {}
        for trial_id in trial_ids:
            parsed = _parse_id(trial_id)
            if parsed is None:
                continue
            catalogs[trial_id] = self.queries.value_catalog(
                Selection(project=project, trials=(parsed,))
            )
        return catalogs

    def sweep_executions(self, selection: Selection) -> list[ExecutionRecord]:
        """Every execution under the selection's sweeps, with derived
        monitoring, for the sweep page's execution list."""
        return self.queries.executions(selection)

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
        self, selection: Selection, keys: tuple[str, ...]
    ) -> list[ValueRecord]:
        """Key-filtered values only; context discovery never follows pages."""
        return self._follow_pages(
            lambda sel, page, token: self.queries.values(
                sel, keys=keys, page=page, page_token=token
            ),
            selection,
        )

    def _follow_params(
        self, selection: Selection, kinds: tuple[str, ...] = ()
    ) -> list[TrialParamRecord]:
        return self._follow_pages(
            lambda sel, page, token: self.queries.trial_params(
                sel, kinds=kinds or None, page=page, page_token=token
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

    def analysis_context_catalog(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> list[dict[str, Any]]:
        """Flat context dimensions under the scope: one DISTINCT-SQL row
        per key with every distinct formatted value (filter options),
        cardinality, and samples — never a paginated values read."""
        if not project:
            return []
        return self.queries.context_catalog(self.analysis_selection(project, tray))

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

    def analysis_finals(
        self, project: str | None, tray: dict[str, Any] | None
    ) -> dict[str, dict[str, Any]]:
        """Per trial, the last logged value of every scalar key — the
        trial's final scalars, stepped or not. Later recorded values win
        within a step, retry generations included."""
        if not project:
            return {}
        selection = self.analysis_selection(project, tray)
        keys = tuple(
            record.key
            for record in self.queries.value_catalog(selection)
            if record.kind == "scalar"
        )
        finals: dict[str, dict[str, tuple[int, Any]]] = {}
        for record in self._follow_values(selection, keys):
            payload = (
                record.observation if record.observation is not None else record.value
            )
            per_trial = finals.setdefault(str(record.trial_id), {})
            current = per_trial.get(record.key)
            if current is None or record.step >= current[0]:
                per_trial[record.key] = (record.step, payload)
        return {
            trial_id: {key: payload for key, (_, payload) in per_trial.items()}
            for trial_id, per_trial in finals.items()
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
