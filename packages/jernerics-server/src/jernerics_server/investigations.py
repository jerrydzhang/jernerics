import json
import re
import time
import uuid
from collections.abc import Sequence
from typing import Literal

from jernerics_schema import JERNERICS_NAMESPACE, InvestigationRecord
from pydantic import BaseModel, ConfigDict

from .store import InvestigationNotFoundError, Store

FactorKind = Literal["manual_param", "config_source", "name_token"]
WarningKind = Literal[
    "unknown_sweep",
    "cross_project_sweep",
    "git_hash_divergence",
    "config_source_divergence",
]

_FACTOR_KINDS: tuple[str, ...] = ("manual_param", "config_source", "name_token")


class FactorCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: FactorKind
    name: str
    members: int


class OutcomeCandidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    members: int


class PreviewWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: WarningKind
    detail: str


class InvestigationPreview(BaseModel):
    model_config = ConfigDict(frozen=True)

    project: str
    member_count: int
    factors: tuple[FactorCandidate, ...]
    outcomes: tuple[OutcomeCandidate, ...]
    warnings: tuple[PreviewWarning, ...]


class InvestigationCoverage(BaseModel):
    model_config = ConfigDict(frozen=True)

    members: int
    with_outcome: int
    completed: int
    invalid: int
    last_activity_ns: int | None


class InvestigationDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    investigation: InvestigationRecord
    coverage: InvestigationCoverage


class InvestigationService:
    """Every investigation read and mutation, shared by HTTP and dashboard."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def list_for_project(
        self, project: str, *, include_archived: bool = False
    ) -> list[InvestigationRecord]:
        return [
            self._record(data)
            for data in self._store.investigations(
                project, include_archived=include_archived
            )
        ]

    def detail(self, investigation_id: str) -> InvestigationDetail:
        return self._detail(self._require(investigation_id))

    def detail_by_name(self, project: str, name: str) -> InvestigationDetail:
        data = self._store.investigation_by_name(project, name)
        if data is None:
            raise InvestigationNotFoundError(
                f"no investigation named {name!r} in project {project!r}"
            )
        return self._detail(self._record(data))

    def create(
        self,
        project: str,
        name: str,
        factor: str,
        outcome: str,
        *,
        replicate_factor: str | None = None,
        members: Sequence[str] = (),
    ) -> InvestigationRecord:
        investigation_id = str(
            uuid.uuid5(JERNERICS_NAMESPACE, f"investigation:{project}:{name}")
        )
        data = self._store.create_investigation(
            investigation_id,
            project,
            name,
            factor,
            outcome,
            replicate_factor,
            members,
        )
        return self._record(data)

    def set_members(
        self, investigation_id: str, sweep_ids: Sequence[str]
    ) -> InvestigationRecord:
        self._store.set_investigation_members(investigation_id, sweep_ids)
        return self._require(investigation_id)

    def add_members(
        self, investigation_id: str, sweep_ids: Sequence[str]
    ) -> InvestigationRecord:
        self._store.add_members(investigation_id, sweep_ids)
        return self._require(investigation_id)

    def remove_members(
        self, investigation_id: str, sweep_ids: Sequence[str]
    ) -> InvestigationRecord:
        self._store.remove_members(investigation_id, sweep_ids)
        return self._require(investigation_id)

    def archive(self, investigation_id: str) -> InvestigationRecord:
        self._store.archive_investigation(investigation_id, time.time_ns())
        return self._require(investigation_id)

    def restore(self, investigation_id: str) -> InvestigationRecord:
        self._store.restore_investigation(investigation_id)
        return self._require(investigation_id)

    def preview(self, project: str, sweep_ids: Sequence[str]) -> InvestigationPreview:
        ids = list(dict.fromkeys(sweep_ids))
        rows: dict[str, tuple[str, str]] = {}
        if ids:
            _, found = self._store.query(
                "SELECT sweep_id, project, name FROM sweeps "
                f"WHERE sweep_id IN ({_placeholders(len(ids))})",
                ids,
            )
            rows = {
                sweep_id: (sweep_project, name)
                for sweep_id, sweep_project, name in found
            }
        unknown = sorted(sid for sid in ids if sid not in rows)
        foreign = sorted(sid for sid in rows if rows[sid][0] != project)
        members = [sid for sid in ids if rows.get(sid, ("", ""))[0] == project]
        warnings = [
            PreviewWarning(kind="unknown_sweep", detail=f"no sweep with id {sid}")
            for sid in unknown
        ] + [
            PreviewWarning(
                kind="cross_project_sweep",
                detail=(
                    f"sweep {sid} belongs to project {rows[sid][0]!r}, not {project!r}"
                ),
            )
            for sid in foreign
        ]
        warnings.extend(self._divergence_warnings(members))
        return InvestigationPreview(
            project=project,
            member_count=len(members),
            factors=self._factor_candidates(members),
            outcomes=self._outcome_candidates(members),
            warnings=tuple(warnings),
        )

    def _divergence_warnings(self, members: Sequence[str]) -> list[PreviewWarning]:
        if not members:
            return []
        warnings: list[PreviewWarning] = []
        for kind, column in (
            ("git_hash_divergence", "git_hash"),
            ("config_source_divergence", "config_source"),
        ):
            _, rows = self._store.query(
                f"SELECT DISTINCT s.{column} FROM submissions s "
                f"WHERE s.{column} IS NOT NULL "
                f"AND s.sweep_id IN ({_placeholders(len(members))}) "
                f"ORDER BY s.{column}",
                list(members),
            )
            values = sorted({row[0] for row in rows})
            if len(values) > 1:
                warnings.append(
                    PreviewWarning(
                        kind=kind,
                        detail=(
                            f"differing {column} across members: {', '.join(values)}"
                        ),
                    )
                )
        return warnings

    def _factor_candidates(self, members: Sequence[str]) -> tuple[FactorCandidate, ...]:
        if not members:
            return ()
        carriers: dict[tuple[FactorKind, str], set[str]] = {}
        _, rows = self._store.query(
            "SELECT tp.key, t.sweep_id, tp.value_json FROM trial_params tp "
            "JOIN trials t ON t.trial_id = tp.trial_id "
            "WHERE tp.kind = 'manual' AND t.state = 'completed' "
            f"AND t.sweep_id IN ({_placeholders(len(members))})",
            list(members),
        )
        numeric_keys: set[str] = set()
        param_carriers: dict[str, set[str]] = {}
        for key, sweep_id, value_json in rows:
            if not _categorical(json.loads(value_json)):
                numeric_keys.add(key)
            param_carriers.setdefault(key, set()).add(sweep_id)
        for key, ids in param_carriers.items():
            if key not in numeric_keys:
                carriers["manual_param", key] = ids
        _, rows = self._store.query(
            "SELECT sweep_id FROM submissions "
            "WHERE config_source IS NOT NULL "
            f"AND sweep_id IN ({_placeholders(len(members))})",
            list(members),
        )
        carriers["config_source", "config_source"] = {row[0] for row in rows}
        _, rows = self._store.query(
            "SELECT sweep_id, name FROM sweeps "
            f"WHERE sweep_id IN ({_placeholders(len(members))})",
            list(members),
        )
        token_carriers: dict[str, set[str]] = {}
        for sweep_id, name in rows:
            for token in _name_tokens(name):
                token_carriers.setdefault(token, set()).add(sweep_id)
        for token, ids in token_carriers.items():
            carriers["name_token", token] = ids
        ordered = sorted(
            carriers.items(),
            key=lambda item: (_FACTOR_KINDS.index(item[0][0]), item[0][1]),
        )
        return tuple(
            FactorCandidate(kind=kind, name=name, members=len(ids))
            for (kind, name), ids in ordered
        )

    def _outcome_candidates(
        self, members: Sequence[str]
    ) -> tuple[OutcomeCandidate, ...]:
        if not members:
            return ()
        _, rows = self._store.query(
            "SELECT tv.key, t.sweep_id FROM tracked_values tv "
            "JOIN executions e ON e.execution_id = tv.execution_id "
            "JOIN trials t ON t.trial_id = e.trial_id "
            "WHERE tv.value_type = 'scalar' AND t.state = 'completed' "
            f"AND t.sweep_id IN ({_placeholders(len(members))})",
            list(members),
        )
        carriers: dict[str, set[str]] = {}
        for key, sweep_id in rows:
            carriers.setdefault(key, set()).add(sweep_id)
        ordered = sorted(carriers.items(), key=lambda item: (-len(item[1]), item[0]))
        return tuple(
            OutcomeCandidate(key=key, members=len(ids)) for key, ids in ordered
        )

    def _coverage(self, record: InvestigationRecord) -> InvestigationCoverage:
        members = [str(sweep_id) for sweep_id in record.members]
        if not members:
            return InvestigationCoverage(
                members=0,
                with_outcome=0,
                completed=0,
                invalid=0,
                last_activity_ns=None,
            )
        _, rows = self._store.query(
            "SELECT s.state, s.updated_ns, c.invalid_ns IS NOT NULL FROM sweeps s "
            "LEFT JOIN sweep_curation c ON c.sweep_id = s.sweep_id "
            f"WHERE s.sweep_id IN ({_placeholders(len(members))})",
            members,
        )
        completed = sum(1 for state, _, _ in rows if state == "completed")
        invalid = sum(1 for _, _, flagged in rows if flagged)
        last_activity = max(updated for _, updated, _ in rows)
        _, outcome_rows = self._store.query(
            "SELECT COUNT(DISTINCT t.sweep_id) FROM trials t "
            "JOIN executions e ON e.trial_id = t.trial_id "
            "JOIN tracked_values tv ON tv.execution_id = e.execution_id "
            "WHERE t.state = 'completed' AND tv.value_type = 'scalar' "
            "AND tv.key = ? "
            f"AND t.sweep_id IN ({_placeholders(len(members))})",
            [record.outcome, *members],
        )
        return InvestigationCoverage(
            members=len(members),
            with_outcome=outcome_rows[0][0],
            completed=completed,
            invalid=invalid,
            last_activity_ns=last_activity,
        )

    def _detail(self, record: InvestigationRecord) -> InvestigationDetail:
        return InvestigationDetail(
            investigation=record, coverage=self._coverage(record)
        )

    def _require(self, investigation_id: str) -> InvestigationRecord:
        data = self._store.investigation(investigation_id)
        if data is None:
            raise InvestigationNotFoundError(
                f"no investigation with id {investigation_id}"
            )
        return self._record(data)

    def _record(self, data: dict) -> InvestigationRecord:
        return InvestigationRecord(
            id=data["investigation_id"],
            project=data["project"],
            name=data["name"],
            factor=data["factor"],
            outcome=data["outcome"],
            replicate_factor=data["replicate_factor"],
            archived_ns=data["archived_ns"],
            created_ns=data["created_ns"],
            updated_ns=data["updated_ns"],
            members=data["members"],
        )


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


def _categorical(value: object) -> bool:
    return isinstance(value, (str, bool))


def _name_tokens(name: str) -> list[str]:
    return [
        token
        for token in re.split(r"[^0-9A-Za-z]+", name)
        if token and not token.isdigit()
    ]
