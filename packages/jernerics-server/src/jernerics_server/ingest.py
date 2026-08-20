"""Validated, atomic materialization of v3 tracking events into the store."""

import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jernerics_schema import (
    ArtifactDeclarationEvent,
    ConflictRecord,
    Event,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionProgressEvent,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    JobSnapshotEvent,
    ManualParamEvent,
    ScalarValue,
    SubmissionSnapshotEvent,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
)

from .store import Store

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

OPTIMIZER_TERMINAL_STATE = "optimizer_terminal_state"

_TERMINAL_TRIAL_STATES = frozenset(
    {TrialState.COMPLETED, TrialState.FAILED, TrialState.PRUNED}
)

_TIER: dict[str, int] = {
    "sweep_snapshot": 0,
    "submission_snapshot": 1,
    "job_snapshot": 2,
    "trial_snapshot": 3,
    "execution_start": 4,
    "execution_heartbeat": 5,
    "execution_progress": 5,
    "manual_param": 5,
    "value": 5,
    "execution_end": 6,
    "artifact_declaration": 7,
}


def _to_ns(value: datetime) -> int:
    """Exact datetime-to-integer-nanoseconds conversion (no float loss)."""
    delta = value - _EPOCH
    return (
        delta.days * 86_400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1_000


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _optional_json(context: FlatContext | None) -> str | None:
    return _canonical_json(context.root) if context is not None else None


def _opt_str(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


class IngestServiceError(Exception):
    """Base for typed ingest failures; carries the offending event."""

    error_code = "ingest_error"

    def __init__(
        self,
        detail: str,
        *,
        event_index: int | None = None,
        event_id: uuid.UUID | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.event_index = event_index
        self.event_id = event_id


class IngestValidationError(IngestServiceError):
    """A precondition for applying the event does not hold (unknown entity)."""

    error_code = "validation"


class IngestConflictError(IngestServiceError):
    """The event disagrees with immutable state; the whole batch is rejected."""

    error_code = "conflict"


@dataclass(frozen=True)
class IngestResult:
    applied: int
    duplicates: int
    conflicts: tuple[ConflictRecord, ...] = ()


def _canonical_order(events: Sequence[Event]) -> list[tuple[int, Event]]:
    tiers: list[list[tuple[int, Event]]] = [[] for _ in range(max(_TIER.values()) + 1)]
    trial_pairs: list[tuple[int, TrialSnapshotEvent]] = []
    for index, event in enumerate(events):
        if isinstance(event, TrialSnapshotEvent):
            trial_pairs.append((index, event))
        else:
            tiers[_TIER[event.tag]].append((index, event))
    ordered_trials = _lineage_order(trial_pairs)
    ordered: list[tuple[int, Event]] = []
    for tier, tier_pairs in enumerate(tiers):
        if tier == _TIER["trial_snapshot"]:
            ordered.extend(ordered_trials)
        ordered.extend(tier_pairs)
    return ordered


def _lineage_order(
    pairs: list[tuple[int, TrialSnapshotEvent]],
) -> list[tuple[int, TrialSnapshotEvent]]:
    """Order trial snapshots so retry parents precede their children.

    A retry's ``retry_of_trial_id``/``retry_root_trial_id`` foreign keys
    require the parent trial row to exist, so parents in the same batch are
    materialized first regardless of arrival order. Unresolvable cycles
    fall back to arrival order and surface as foreign-key conflicts.
    """
    batch_ids = {pair[1].trial_id for pair in pairs}
    placed: set[uuid.UUID] = set()
    ordered: list[tuple[int, TrialSnapshotEvent]] = []
    remaining = list(pairs)
    while remaining:
        deferred: list[tuple[int, TrialSnapshotEvent]] = []
        progressed = False
        for pair in remaining:
            event = pair[1]
            parents = {event.retry_of_trial_id, event.retry_root_trial_id}
            parents.discard(event.trial_id)
            if (parents & batch_ids) - placed:
                deferred.append(pair)
            else:
                ordered.append(pair)
                placed.add(event.trial_id)
                progressed = True
        if not progressed:
            ordered.extend(deferred)
            break
        remaining = deferred
    return ordered


class IngestService:
    """Applies validated event batches atomically to the store."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def apply(self, request: IngestRequest) -> IngestResult:
        with self._store._lock:
            con = self._store._con
            con.execute("BEGIN IMMEDIATE")
            try:
                applied = 0
                duplicates = 0
                conflicts: list[ConflictRecord] = []
                for index, event in _canonical_order(request.events):
                    if self._dispatch(con, index, event, conflicts):
                        applied += 1
                    else:
                        duplicates += 1
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        return IngestResult(
            applied=applied, duplicates=duplicates, conflicts=tuple(conflicts)
        )

    def _dispatch(
        self,
        con: sqlite3.Connection,
        index: int,
        event: Event,
        conflicts: list[ConflictRecord],
    ) -> bool:
        try:
            return self._apply(con, index, event, conflicts)
        except sqlite3.IntegrityError as e:
            raise IngestConflictError(
                str(e), event_index=index, event_id=event.event_id
            ) from e

    def _apply(
        self,
        con: sqlite3.Connection,
        index: int,
        event: Event,
        conflicts: list[ConflictRecord],
    ) -> bool:
        match event:
            case SweepSnapshotEvent():
                return self._apply_sweep_snapshot(con, index, event)
            case SubmissionSnapshotEvent():
                return self._apply_submission_snapshot(con, index, event)
            case JobSnapshotEvent():
                return self._apply_job_snapshot(con, index, event)
            case TrialSnapshotEvent():
                return self._apply_trial_snapshot(con, index, event, conflicts)
            case ExecutionStartEvent():
                return self._apply_execution_start(con, index, event)
            case ExecutionHeartbeatEvent():
                return self._apply_execution_heartbeat(con, index, event)
            case ExecutionProgressEvent():
                return self._apply_execution_progress(con, index, event)
            case ManualParamEvent():
                return self._apply_manual_param(con, index, event)
            case ValueEvent():
                return self._apply_value(con, index, event)
            case ExecutionEndEvent():
                return self._apply_execution_end(con, index, event)
            case ArtifactDeclarationEvent():
                return self._apply_artifact_declaration(con, index, event)
        raise IngestValidationError(
            f"unsupported event tag {event.tag!r}",
            event_index=index,
            event_id=event.event_id,
        )

    def _conflict(self, index: int, event: Event, detail: str) -> IngestConflictError:
        return IngestConflictError(detail, event_index=index, event_id=event.event_id)

    def _invalid(self, index: int, event: Event, detail: str) -> IngestValidationError:
        return IngestValidationError(detail, event_index=index, event_id=event.event_id)

    def _trial_exists(self, con: sqlite3.Connection, trial_id: uuid.UUID) -> bool:
        return (
            con.execute(
                "SELECT 1 FROM trials WHERE trial_id = ?", [str(trial_id)]
            ).fetchone()
            is not None
        )

    def _apply_sweep_snapshot(
        self, con: sqlite3.Connection, index: int, event: SweepSnapshotEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        row = con.execute(
            "SELECT project, name, state, updated_ns FROM sweeps WHERE sweep_id = ?",
            [str(event.sweep_id)],
        ).fetchone()
        if row is None:
            clash = con.execute(
                "SELECT sweep_id FROM sweeps WHERE project = ? AND name = ?",
                [event.project, event.name],
            ).fetchone()
            if clash is not None:
                raise self._conflict(
                    index,
                    event,
                    f"sweep {event.sweep_id} reuses (project, name) = "
                    f"({event.project!r}, {event.name!r}) already held by "
                    f"sweep {clash[0]}",
                )
            con.execute(
                "INSERT INTO sweeps (sweep_id, project, name, state, "
                "created_ns, updated_ns) VALUES (?, ?, ?, ?, ?, ?)",
                [str(event.sweep_id), event.project, event.name, event.state, ns, ns],
            )
            return True
        project, name, state, updated_ns = row
        if (project, name) != (event.project, event.name):
            raise self._conflict(
                index,
                event,
                f"sweep {event.sweep_id} is immutable: stored (project, name) "
                f"= ({project!r}, {name!r}), incoming ({event.project!r}, "
                f"{event.name!r})",
            )
        new_updated = max(updated_ns, ns)
        if state == event.state and new_updated == updated_ns:
            return False
        con.execute(
            "UPDATE sweeps SET state = ?, updated_ns = ? WHERE sweep_id = ?",
            [event.state, new_updated, str(event.sweep_id)],
        )
        return True

    def _apply_submission_snapshot(
        self, con: sqlite3.Connection, index: int, event: SubmissionSnapshotEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        submitted_ns = (
            _to_ns(event.submitted_at) if event.submitted_at is not None else None
        )
        incoming = (
            submitted_ns,
            event.expected_trials,
            event.git_hash,
            event.config_source,
        )
        row = con.execute(
            "SELECT sweep_id, backend, state, submitted_ns, expected_trials, "
            "git_hash, config_source, updated_ns FROM submissions "
            "WHERE submission_id = ?",
            [str(event.submission_id)],
        ).fetchone()
        state_value = event.state.value
        if row is None:
            if (
                con.execute(
                    "SELECT 1 FROM sweeps WHERE sweep_id = ?",
                    [str(event.sweep_id)],
                ).fetchone()
                is None
            ):
                raise self._invalid(
                    index,
                    event,
                    f"submission {event.submission_id} references unknown "
                    f"sweep {event.sweep_id}",
                )
            con.execute(
                "INSERT INTO submissions (submission_id, sweep_id, backend, "
                "state, submitted_ns, expected_trials, git_hash, "
                "config_source, created_ns, updated_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(event.submission_id),
                    str(event.sweep_id),
                    event.backend,
                    state_value,
                    *incoming,
                    ns,
                    ns,
                ],
            )
            return True
        (
            sweep_id,
            backend,
            state,
            submitted,
            expected,
            git_hash,
            config_source,
            updated_ns,
        ) = row
        if sweep_id != str(event.sweep_id):
            raise self._conflict(
                index,
                event,
                f"submission {event.submission_id} sweep is immutable: "
                f"stored {sweep_id}, incoming {event.sweep_id}",
            )
        if backend != event.backend:
            raise self._conflict(
                index,
                event,
                f"submission {event.submission_id} backend is immutable: "
                f"stored {backend!r}, incoming {event.backend!r}",
            )
        stored = (submitted, expected, git_hash, config_source)
        for name, was, now in zip(
            ("submitted_ns", "expected_trials", "git_hash", "config_source"),
            stored,
            incoming,
            strict=True,
        ):
            if was is not None and now is not None and was != now:
                raise self._conflict(
                    index,
                    event,
                    f"submission {event.submission_id} {name} is write-once: "
                    f"stored {was!r}, incoming {now!r}",
                )
        filled = tuple(
            was if was is not None else now
            for was, now in zip(stored, incoming, strict=True)
        )
        new_updated = max(updated_ns, ns)
        if state == state_value and filled == stored and new_updated == updated_ns:
            return False
        con.execute(
            "UPDATE submissions SET state = ?, submitted_ns = ?, "
            "expected_trials = ?, git_hash = ?, config_source = ?, "
            "updated_ns = ? WHERE submission_id = ?",
            [state_value, *filled, new_updated, str(event.submission_id)],
        )
        return True

    def _apply_job_snapshot(
        self, con: sqlite3.Connection, index: int, event: JobSnapshotEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        state_value = event.state.value
        row = con.execute(
            "SELECT submission_id, scheduler_job_id, role, state, updated_ns "
            "FROM submission_jobs WHERE job_id = ?",
            [str(event.job_id)],
        ).fetchone()
        if row is None:
            if (
                con.execute(
                    "SELECT 1 FROM submissions WHERE submission_id = ?",
                    [str(event.submission_id)],
                ).fetchone()
                is None
            ):
                raise self._invalid(
                    index,
                    event,
                    f"job {event.job_id} references unknown submission "
                    f"{event.submission_id}",
                )
            clash = con.execute(
                "SELECT job_id FROM submission_jobs WHERE submission_id = ? "
                "AND scheduler_job_id = ?",
                [str(event.submission_id), event.scheduler_job_id],
            ).fetchone()
            if clash is not None:
                raise self._conflict(
                    index,
                    event,
                    f"job {event.job_id} reuses (submission_id, "
                    f"scheduler_job_id) = ({event.submission_id}, "
                    f"{event.scheduler_job_id}) already held by job {clash[0]}",
                )
            con.execute(
                "INSERT INTO submission_jobs (job_id, submission_id, "
                "scheduler_job_id, role, state, updated_ns) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    str(event.job_id),
                    str(event.submission_id),
                    event.scheduler_job_id,
                    event.role,
                    state_value,
                    ns,
                ],
            )
            return True
        submission_id, scheduler_job_id, role, state, updated_ns = row
        if submission_id != str(event.submission_id):
            raise self._conflict(
                index,
                event,
                f"job {event.job_id} submission is immutable: stored "
                f"{submission_id}, incoming {event.submission_id}",
            )
        if scheduler_job_id != event.scheduler_job_id:
            raise self._conflict(
                index,
                event,
                f"job {event.job_id} scheduler_job_id is immutable: stored "
                f"{scheduler_job_id!r}, incoming {event.scheduler_job_id!r}",
            )
        if role is not None and role != event.role:
            raise self._conflict(
                index,
                event,
                f"job {event.job_id} role is immutable: stored {role!r}, "
                f"incoming {event.role!r}",
            )
        filled_role = role if role is not None else event.role
        new_updated = max(updated_ns, ns)
        if state == state_value and filled_role == role and new_updated == updated_ns:
            return False
        con.execute(
            "UPDATE submission_jobs SET role = ?, state = ?, updated_ns = ? "
            "WHERE job_id = ?",
            [filled_role, state_value, new_updated, str(event.job_id)],
        )
        return True

    def _apply_trial_snapshot(
        self,
        con: sqlite3.Connection,
        index: int,
        event: TrialSnapshotEvent,
        conflicts: list[ConflictRecord],
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        # Flat distributions/attrs travel as canonical JSON strings in the
        # trials row; None means the snapshot does not carry them.
        distributions_json = _optional_json(event.distributions)
        attrs_json = _optional_json(event.attrs)
        row = con.execute(
            "SELECT sweep_id, number, state, retry_of_trial_id, "
            "retry_root_trial_id, retry_index, objective, distributions_json, "
            "attrs_json, updated_ns FROM trials WHERE trial_id = ?",
            [str(event.trial_id)],
        ).fetchone()
        changed = False
        if row is None:
            if (
                con.execute(
                    "SELECT 1 FROM sweeps WHERE sweep_id = ?",
                    [str(event.sweep_id)],
                ).fetchone()
                is None
            ):
                raise self._invalid(
                    index,
                    event,
                    f"trial {event.trial_id} references unknown sweep {event.sweep_id}",
                )
            holder = con.execute(
                "SELECT trial_id FROM trials WHERE sweep_id = ? AND number = ?",
                [str(event.sweep_id), event.number],
            ).fetchone()
            if holder is not None:
                raise self._conflict(
                    index,
                    event,
                    f"trial number {event.number} in sweep {event.sweep_id} "
                    f"is already held by trial {holder[0]}",
                )
            for parent in (event.retry_of_trial_id, event.retry_root_trial_id):
                if (
                    parent is not None
                    and parent != event.trial_id
                    and not self._trial_exists(con, parent)
                ):
                    raise self._invalid(
                        index,
                        event,
                        f"trial {event.trial_id} references unknown retry "
                        f"parent {parent}",
                    )
            con.execute(
                "INSERT INTO trials (trial_id, sweep_id, number, state, "
                "retry_of_trial_id, retry_root_trial_id, retry_index, "
                "objective, distributions_json, attrs_json, "
                "created_ns, updated_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    str(event.trial_id),
                    str(event.sweep_id),
                    event.number,
                    event.state.value,
                    _opt_str(event.retry_of_trial_id),
                    str(event.retry_root_trial_id),
                    event.retry_index,
                    event.objective,
                    distributions_json,
                    attrs_json,
                    ns,
                    ns,
                ],
            )
            changed = True
        else:
            (
                sweep_id,
                number,
                state,
                retry_of,
                retry_root,
                retry_index,
                objective,
                stored_distributions,
                stored_attrs,
                updated_ns,
            ) = row
            if sweep_id != str(event.sweep_id) or number != event.number:
                raise self._conflict(
                    index,
                    event,
                    f"trial {event.trial_id} identity is immutable: stored "
                    f"(sweep, number) = ({sweep_id}, {number}), incoming "
                    f"({event.sweep_id}, {event.number})",
                )
            lineage = (
                _opt_str(event.retry_of_trial_id),
                str(event.retry_root_trial_id),
                event.retry_index,
            )
            if (retry_of, retry_root, retry_index) != lineage:
                raise self._conflict(
                    index,
                    event,
                    f"trial {event.trial_id} retry lineage is immutable: "
                    f"stored (retry_of={retry_of}, retry_root={retry_root}, "
                    f"retry_index={retry_index}), incoming "
                    f"(retry_of={lineage[0]}, retry_root={lineage[1]}, "
                    f"retry_index={lineage[2]})",
                )
            stored_state = TrialState(state)
            state_conflict = False
            if stored_state in _TERMINAL_TRIAL_STATES:
                if event.state != stored_state:
                    state_conflict = True
                    changed = self._record_optimizer_conflict(
                        con, ns, stored_state, event, conflicts
                    )
            else:
                new_updated = max(updated_ns, ns)
                if state != event.state.value or new_updated != updated_ns:
                    con.execute(
                        "UPDATE trials SET state = ?, updated_ns = ? "
                        "WHERE trial_id = ?",
                        [event.state.value, new_updated, str(event.trial_id)],
                    )
                    changed = True
            if not state_conflict:
                fills = {}
                for column, stored_value, incoming in (
                    ("objective", objective, event.objective),
                    ("distributions_json", stored_distributions, distributions_json),
                    ("attrs_json", stored_attrs, attrs_json),
                ):
                    if incoming is None or stored_value == incoming:
                        continue
                    if stored_value is not None:
                        raise self._conflict(
                            index,
                            event,
                            f"trial {event.trial_id} {column} is write-once: "
                            f"stored {stored_value!r}, incoming {incoming!r}",
                        )
                    fills[column] = incoming
                if fills:
                    # Terminal facts stay whole: a snapshot whose state
                    # conflicted never writes objective/distributions/attrs.
                    con.execute(
                        f"UPDATE trials SET {', '.join(f'{k} = ?' for k in fills)}, "
                        "updated_ns = max(updated_ns, ?) WHERE trial_id = ?",
                        [*fills.values(), ns, str(event.trial_id)],
                    )
                    changed = True
        for key, value in event.params.root.items():
            if self._write_param(
                con, index, event, str(event.trial_id), "sampled", key, value, ns
            ):
                changed = True
        return changed

    def _record_optimizer_conflict(
        self,
        con: sqlite3.Connection,
        ns: int,
        stored_state: TrialState,
        event: TrialSnapshotEvent,
        conflicts: list[ConflictRecord],
    ) -> bool:
        detail = _canonical_json(
            {"existing": stored_state.value, "incoming": event.state.value}
        )
        already = con.execute(
            "SELECT 1 FROM reconciliation_conflicts WHERE trial_id = ? "
            "AND kind = ? AND detail = ?",
            [str(event.trial_id), OPTIMIZER_TERMINAL_STATE, detail],
        ).fetchone()
        if already is not None:
            return False
        con.execute(
            "INSERT INTO reconciliation_conflicts (trial_id, kind, detail, "
            "detected_ns) VALUES (?, ?, ?, ?)",
            [str(event.trial_id), OPTIMIZER_TERMINAL_STATE, detail, ns],
        )
        conflicts.append(
            ConflictRecord(
                trial_id=event.trial_id,
                kind=OPTIMIZER_TERMINAL_STATE,
                detail=detail,
            )
        )
        return True

    def _write_param(
        self,
        con: sqlite3.Connection,
        index: int,
        event: Event,
        trial_id: str,
        kind: str,
        key: str,
        value: ScalarValue,
        ns: int,
    ) -> bool:
        value_json = _canonical_json(value)
        row = con.execute(
            "SELECT value_json FROM trial_params WHERE trial_id = ? "
            "AND kind = ? AND key = ?",
            [trial_id, kind, key],
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO trial_params (trial_id, kind, key, value_json, "
                "updated_ns) VALUES (?, ?, ?, ?, ?)",
                [trial_id, kind, key, value_json, ns],
            )
            return True
        if row[0] == value_json:
            return False
        raise self._conflict(
            index,
            event,
            f"param ({key!r}, kind {kind!r}) for trial {trial_id} is "
            f"write-once: stored {row[0]}, incoming {value_json}",
        )

    def _apply_manual_param(
        self, con: sqlite3.Connection, index: int, event: ManualParamEvent
    ) -> bool:
        if not self._trial_exists(con, event.trial_id):
            raise self._invalid(
                index,
                event,
                f"manual param references unknown trial {event.trial_id}",
            )
        return self._write_param(
            con,
            index,
            event,
            str(event.trial_id),
            "manual",
            event.key,
            event.value,
            _to_ns(event.recorded_at),
        )

    def _apply_execution_start(
        self, con: sqlite3.Connection, index: int, event: ExecutionStartEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        started_ns = _to_ns(event.started_at)
        row = con.execute(
            "SELECT trial_id, hostname, started_ns FROM executions "
            "WHERE execution_id = ?",
            [str(event.execution_id)],
        ).fetchone()
        if row is None:
            if not self._trial_exists(con, event.trial_id):
                raise self._invalid(
                    index,
                    event,
                    f"execution {event.execution_id} references unknown "
                    f"trial {event.trial_id}",
                )
            con.execute(
                "INSERT INTO executions (execution_id, trial_id, hostname, "
                "started_ns, created_ns, updated_ns) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    str(event.execution_id),
                    str(event.trial_id),
                    event.hostname,
                    started_ns,
                    ns,
                    ns,
                ],
            )
            return True
        if (row[0], row[1], row[2]) != (
            str(event.trial_id),
            event.hostname,
            started_ns,
        ):
            raise self._conflict(
                index,
                event,
                f"execution {event.execution_id} already exists with differing facts",
            )
        return False

    def _apply_execution_heartbeat(
        self, con: sqlite3.Connection, index: int, event: ExecutionHeartbeatEvent
    ) -> bool:
        at_ns = _to_ns(event.at)
        row = con.execute(
            "SELECT last_heartbeat_ns FROM executions WHERE execution_id = ?",
            [str(event.execution_id)],
        ).fetchone()
        if row is None:
            raise self._invalid(
                index,
                event,
                f"heartbeat references unknown execution {event.execution_id}",
            )
        if row[0] is not None and row[0] >= at_ns:
            return False
        con.execute(
            "UPDATE executions SET last_heartbeat_ns = ?, "
            "updated_ns = max(updated_ns, ?) WHERE execution_id = ?",
            [at_ns, at_ns, str(event.execution_id)],
        )
        return True

    def _apply_execution_progress(
        self, con: sqlite3.Connection, index: int, event: ExecutionProgressEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        if (
            con.execute(
                "SELECT 1 FROM executions WHERE execution_id = ?",
                [str(event.execution_id)],
            ).fetchone()
            is None
        ):
            raise self._invalid(
                index,
                event,
                f"progress references unknown execution {event.execution_id}",
            )
        row = con.execute(
            "SELECT updated_ns FROM execution_progress WHERE execution_id = ?",
            [str(event.execution_id)],
        ).fetchone()
        if row is None:
            con.execute(
                "INSERT INTO execution_progress (execution_id, current, "
                "total, unit, updated_ns) VALUES (?, ?, ?, ?, ?)",
                [
                    str(event.execution_id),
                    event.current,
                    event.total,
                    event.unit,
                    ns,
                ],
            )
            return True
        if ns <= row[0]:
            return False
        con.execute(
            "UPDATE execution_progress SET current = ?, total = ?, unit = ?, "
            "updated_ns = ? WHERE execution_id = ?",
            [
                event.current,
                event.total,
                event.unit,
                ns,
                str(event.execution_id),
            ],
        )
        return True

    def _apply_execution_end(
        self, con: sqlite3.Connection, index: int, event: ExecutionEndEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        ended_ns = _to_ns(event.ended_at)
        row = con.execute(
            "SELECT ended_ns, outcome, exit_code, failure_kind, "
            "failure_summary, updated_ns FROM executions WHERE execution_id = ?",
            [str(event.execution_id)],
        ).fetchone()
        if row is None:
            raise self._invalid(
                index,
                event,
                f"execution_end references unknown execution {event.execution_id}",
            )
        ended, outcome, exit_code, failure_kind, failure_summary, _ = row
        incoming = (
            ended_ns,
            event.outcome.value,
            event.exit_code,
            event.failure_kind.value if event.failure_kind is not None else None,
            event.failure_summary,
        )
        if ended is not None:
            if (ended, outcome, exit_code, failure_kind, failure_summary) == incoming:
                return False
            raise self._conflict(
                index,
                event,
                f"execution {event.execution_id} is terminal with immutable "
                f"facts: stored (ended_ns={ended}, outcome={outcome}, "
                f"exit_code={exit_code}, failure_kind={failure_kind}), "
                f"incoming (ended_ns={incoming[0]}, outcome={incoming[1]}, "
                f"exit_code={incoming[2]}, failure_kind={incoming[3]})",
            )
        con.execute(
            "UPDATE executions SET ended_ns = ?, outcome = ?, exit_code = ?, "
            "failure_kind = ?, failure_summary = ?, "
            "updated_ns = max(updated_ns, ?) WHERE execution_id = ?",
            [
                incoming[0],
                incoming[1],
                incoming[2],
                incoming[3],
                incoming[4],
                ns,
                str(event.execution_id),
            ],
        )
        return True

    def _resolve_execution(
        self, con: sqlite3.Connection, index: int, event: ValueEvent
    ) -> str:
        """Resolve which execution a value event belongs to.

        ValueEvent carries ``trial_id`` but tracked_values keys on
        ``execution_id``: prefer the trial's ACTIVE execution (ended_ns IS
        NULL, latest started_ns); if none is active, the trial's most
        recently started execution; a trial with no execution at all is a
        structured conflict.
        """
        row = con.execute(
            "SELECT execution_id FROM executions WHERE trial_id = ? "
            "AND ended_ns IS NULL ORDER BY started_ns DESC LIMIT 1",
            [str(event.trial_id)],
        ).fetchone()
        if row is None:
            row = con.execute(
                "SELECT execution_id FROM executions WHERE trial_id = ? "
                "ORDER BY started_ns DESC LIMIT 1",
                [str(event.trial_id)],
            ).fetchone()
        if row is None:
            raise self._conflict(
                index,
                event,
                f"value for trial {event.trial_id} with no execution",
            )
        return row[0]

    def _apply_value(
        self, con: sqlite3.Connection, index: int, event: ValueEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        execution_id = self._resolve_execution(con, index, event)
        value_type, scalar_val, text_val = _encode_payload(event)
        context = (
            _canonical_json(event.context.root) if event.context is not None else "{}"
        )
        row = con.execute(
            "SELECT value_type, scalar_val, text_val, context, recorded_ns "
            "FROM tracked_values WHERE execution_id = ? AND key = ? AND step = ?",
            [execution_id, event.key, event.step],
        ).fetchone()
        changed = False
        if row is None:
            con.execute(
                "INSERT INTO tracked_values (execution_id, key, step, "
                "value_type, scalar_val, text_val, context, recorded_ns) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    execution_id,
                    event.key,
                    event.step,
                    value_type,
                    scalar_val,
                    text_val,
                    context,
                    ns,
                ],
            )
            changed = True
        elif tuple(row) != (value_type, scalar_val, text_val, context, ns):
            raise self._conflict(
                index,
                event,
                f"value ({event.key!r}, step {event.step}) for execution "
                f"{execution_id} is immutable: stored {tuple(row)}, incoming "
                f"{(value_type, scalar_val, text_val, context, ns)}",
            )
        observation = con.execute(
            "SELECT last_observation_ns FROM executions WHERE execution_id = ?",
            [execution_id],
        ).fetchone()
        if observation[0] is None or observation[0] < ns:
            con.execute(
                "UPDATE executions SET last_observation_ns = ?, "
                "updated_ns = max(updated_ns, ?) WHERE execution_id = ?",
                [ns, ns, execution_id],
            )
            changed = True
        return changed

    def _apply_artifact_declaration(
        self, con: sqlite3.Connection, index: int, event: ArtifactDeclarationEvent
    ) -> bool:
        ns = _to_ns(event.recorded_at)
        row = con.execute(
            "SELECT trial_id, execution_id, key, filename, content_type, "
            "size_bytes, sha256, declared_ns FROM artifacts "
            "WHERE artifact_id = ?",
            [str(event.artifact_id)],
        ).fetchone()
        incoming = (
            str(event.trial_id),
            _opt_str(event.execution_id),
            event.key,
            event.filename,
            event.content_type,
            event.size_bytes,
            event.sha256,
            ns,
        )
        if row is None:
            if not self._trial_exists(con, event.trial_id):
                raise self._invalid(
                    index,
                    event,
                    f"artifact {event.artifact_id} references unknown "
                    f"trial {event.trial_id}",
                )
            if (
                event.execution_id is not None
                and con.execute(
                    "SELECT 1 FROM executions WHERE execution_id = ?",
                    [str(event.execution_id)],
                ).fetchone()
                is None
            ):
                raise self._invalid(
                    index,
                    event,
                    f"artifact {event.artifact_id} references unknown "
                    f"execution {event.execution_id}",
                )
            con.execute(
                "INSERT INTO artifacts (artifact_id, trial_id, execution_id, "
                "key, filename, content_type, size_bytes, sha256, "
                "declared_ns, received_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                [str(event.artifact_id), *incoming],
            )
            return True
        if tuple(row) == incoming:
            return False
        raise self._conflict(
            index,
            event,
            f"artifact {event.artifact_id} already declared with differing facts",
        )


def _encode_payload(event: ValueEvent) -> tuple[str, float | None, str | None]:
    if event.observation is not None:
        return "json", None, _canonical_json(event.observation)
    if isinstance(event.value, bool | str):
        return "json", None, _canonical_json(event.value)
    if event.value is None:
        raise ValueError("value event carries neither value nor observation")
    return "scalar", float(event.value), None
