"""Typed domain queries over the normalized v3 store.

All domain SQL lives here (and only here): HTTP read handlers and future
dashboard callbacks call :class:`QueryService` instead of writing SQL.
Raw expert SQL remains :meth:`Store.query` behind ``/query``.
"""

import json
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from jernerics_schema import (
    ArtifactRecord,
    ArtifactSource,
    ExecutionOutcome,
    ExecutionRecord,
    FailureKind,
    FlatContext,
    JobResourceRecord,
    Page,
    PageToken,
    ProvenanceRecord,
    ScalarValue,
    Selection,
    SweepRecord,
    TrialLineageRecord,
    TrialParamRecord,
    TrialRecord,
    TrialState,
    ValueCatalogRecord,
    ValueRecord,
    decode_page_token,
    encode_page_token,
)

from .store import Store

_CONTEXT_SAMPLE_LIMIT = 5
"""Context-dimension sample values shown in the data catalog."""


_MonitoringLabel = Literal["active", "quiet", "stale", "ended", "unknown"]
_QUIET_FRACTION = 0.25
"""Heartbeats fresher than this fraction of the stale threshold are
"active"; older-but-within-threshold heartbeats are "quiet"."""


class QueryServiceError(Exception):
    """Base for typed domain-query failures; carries a wire error code."""

    code = "query_error"


class InvalidPageTokenError(QueryServiceError):
    """The page token is not a decodable token."""

    code = "invalid_page_token"


class PageTokenMismatchError(QueryServiceError):
    """The token was issued under different filters or a different limit."""

    code = "page_token_mismatch"


class OffsetPaginationUnsupportedError(QueryServiceError):
    """Offset paging was requested where only keyset tokens are supported."""

    code = "offset_unsupported"


def _from_ns(ns: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000)


def _flat(json_text: str | None) -> FlatContext | None:
    if json_text is None:
        return None
    root = json.loads(json_text)
    return FlatContext(root=root) if root else None


def _ids(values: Sequence[uuid.UUID]) -> list[str]:
    return [str(value) for value in values]


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


def _cursor_clause(columns: Sequence[str]) -> str:
    """Strictly-after keyset predicate mirroring the ORDER BY columns."""
    if len(columns) == 1:
        return f"{columns[0]} > ?"
    head, *rest = columns
    return f"({head} > ? OR ({head} = ? AND {_cursor_clause(rest)}))"


def _cursor_params(values: Sequence[int | str]) -> list[int | str]:
    if len(values) == 1:
        return [values[0]]
    return [values[0], values[0], *_cursor_params(values[1:])]


def _echo(selection: Selection, **filters: Any) -> dict[str, Any]:
    """Canonical filter echo embedded in page tokens.

    ``None`` and empty collections mean "no filter" and are dropped;
    ``False`` stays (``received=False`` is a meaningful filter). Tuples
    become lists so token round-trips compare equal.
    """
    echo: dict[str, Any] = {"selection": selection.model_dump(mode="json")}
    for name, value in filters.items():
        if value is None or (isinstance(value, tuple | str) and not value):
            continue
        echo[name] = list(value) if isinstance(value, tuple) else value
    return echo


def _monitoring(
    ended_ns: int | None, last_heartbeat_ns: int | None, heartbeat_stale_s: float
) -> _MonitoringLabel:
    """Derived on read, never persisted: ended wins; no heartbeat fact is
    "unknown"; otherwise active within a quarter of the threshold, quiet
    until it, stale past it."""
    if ended_ns is not None:
        return "ended"
    if last_heartbeat_ns is None:
        return "unknown"
    age_ns = time.time_ns() - last_heartbeat_ns
    threshold_ns = heartbeat_stale_s * 1_000_000_000
    if age_ns <= threshold_ns * _QUIET_FRACTION:
        return "active"
    if age_ns <= threshold_ns:
        return "quiet"
    return "stale"


_ZERO_COUNTS: dict[str, int] = {
    "active": 0,
    "quiet": 0,
    "stale": 0,
    "unknown": 0,
    "succeeded": 0,
    "failed": 0,
}


_SWEEP_CURATED_CTES = (
    "live AS ("
    "SELECT t.sweep_id AS sweep_id, SUM(t.state = 'waiting') AS waiting, "
    "SUM(t.state = 'running') AS running FROM trials t GROUP BY t.sweep_id), "
    "ended AS ("
    "SELECT t.sweep_id AS sweep_id, COUNT(*) AS started, "
    "SUM(e.ended_ns IS NOT NULL) AS terminal FROM executions e "
    "JOIN trials t ON e.trial_id = t.trial_id GROUP BY t.sweep_id), "
    "sweep_curated AS ("
    "SELECT s.sweep_id AS sweep_id, s.project AS project, s.name AS name, "
    "s.updated_ns AS updated_ns, c.archived_ns IS NOT NULL AS archived, "
    "c.invalid_ns IS NOT NULL AS invalid, "
    "COALESCE(live.waiting, 0) > 0 OR COALESCE(live.running, 0) > 0 "
    "OR COALESCE(ended.started, 0) > COALESCE(ended.terminal, 0) AS incomplete "
    "FROM sweeps s LEFT JOIN sweep_curation c ON c.sweep_id = s.sweep_id "
    "LEFT JOIN live ON live.sweep_id = s.sweep_id "
    "LEFT JOIN ended ON ended.sweep_id = s.sweep_id)"
)
_ALL_SWEEPS_CTES = _SWEEP_CURATED_CTES + (
    ", current_sweeps AS (SELECT * FROM sweep_curated)"
)
_CURRENT_SWEEPS_CTES = _SWEEP_CURATED_CTES + (
    ", current_sweeps AS ("
    "SELECT * FROM sweep_curated WHERE incomplete OR NOT (archived OR invalid))"
)
"""CTE chains ending in ``current_sweeps``: every sweep with its curation
facts and the completeness notion :meth:`QueryService.sweep_overview`
computes; a sweep is current while incomplete, or terminal and neither
archived nor invalid. ``_ALL_SWEEPS_CTES`` keeps curated sweeps in so
the historical failure roll-up can include them."""


class QueryService:
    """Every domain read over the v3 store, shared by HTTP and callbacks."""

    def __init__(
        self,
        store: Store,
        *,
        heartbeat_stale_s: float = 900.0,
        artifacts_root: Path | None = None,
    ) -> None:
        self._store = store
        self.heartbeat_stale_s = heartbeat_stale_s
        self._artifacts_root = artifacts_root

    def _trial_id_bits(
        self, selection: Selection, trials_alias: str = "t"
    ) -> tuple[str, list[str]]:
        """OR-ed id filters: named trials, retry families, executions' trials."""
        bits: list[str] = []
        params: list[str] = []
        if selection.trials:
            params.extend(_ids(selection.trials))
            bits.append(
                f"{trials_alias}.trial_id IN ({_placeholders(len(selection.trials))})"
            )
        if selection.retry_roots:
            params.extend(_ids(selection.retry_roots))
            bits.append(
                f"{trials_alias}.retry_root_trial_id IN "
                f"({_placeholders(len(selection.retry_roots))})"
            )
        if selection.executions:
            params.extend(_ids(selection.executions))
            bits.append(
                f"EXISTS (SELECT 1 FROM executions ex WHERE ex.execution_id IN "
                f"({_placeholders(len(selection.executions))}) AND ex.trial_id = "
                f"{trials_alias}.trial_id)"
            )
        return " OR ".join(bits), params

    def _trial_scope(
        self, selection: Selection, trials_alias: str = "t", sweeps_alias: str = "s"
    ) -> tuple[str, list[Any]]:
        """WHERE fragment restricting trials (joined to sweeps) by selection."""
        clauses = [f"{sweeps_alias}.project = ?"]
        params: list[Any] = [selection.project]
        if selection.sweeps:
            params.extend(_ids(selection.sweeps))
            clauses.append(
                f"{trials_alias}.sweep_id IN ({_placeholders(len(selection.sweeps))})"
            )
        id_sql, id_params = self._trial_id_bits(selection, trials_alias)
        if id_sql:
            clauses.append(f"({id_sql})")
            params.extend(id_params)
        return " AND ".join(clauses), params

    def _selected_sweep_ids(self, selection: Selection) -> list[str] | None:
        """Sweeps named directly or holding any selected trial/execution.

        ``None`` means the selection names no id filters (unrestricted).
        """
        id_sql, id_params = self._trial_id_bits(selection)
        if not selection.sweeps and not id_sql:
            return None
        clauses = ["s.project = ?"]
        params: list[Any] = [selection.project]
        if selection.sweeps:
            params.extend(_ids(selection.sweeps))
            clauses.append(f"s.sweep_id IN ({_placeholders(len(selection.sweeps))})")
        if id_sql:
            clauses.append(
                f"EXISTS (SELECT 1 FROM trials t WHERE t.sweep_id = s.sweep_id "
                f"AND ({id_sql}))"
            )
            params.extend(id_params)
        _, rows = self._store.query(
            f"SELECT s.sweep_id FROM sweeps s WHERE {' AND '.join(clauses)}",
            params,
        )
        return [row[0] for row in rows]

    def _fetch_paged(
        self,
        *,
        sql: str,
        params: list[Any],
        order_columns: Sequence[str],
        page: Page,
        page_token: str | None,
        filters: dict[str, Any],
    ) -> tuple[list[tuple], str | None]:
        if page.offset != 0:
            raise OffsetPaginationUnsupportedError(
                "offset pagination is not supported; pass next_token instead"
            )
        cursor: tuple[int | str, ...] | None = None
        if page_token is not None:
            try:
                token = decode_page_token(page_token)
            except ValueError as e:
                raise InvalidPageTokenError(str(e)) from e
            if len(token.cursor) != len(order_columns):
                raise InvalidPageTokenError(
                    f"page token cursor has {len(token.cursor)} components, "
                    f"expected {len(order_columns)}"
                )
            if token.filters != filters or token.limit != page.limit:
                raise PageTokenMismatchError(
                    "page token does not match the current request filters and limit"
                )
            cursor = token.cursor
        if cursor is not None:
            sql += f" AND {_cursor_clause(order_columns)}"
            params.extend(_cursor_params(cursor))
        sql += f" ORDER BY {', '.join(order_columns)} LIMIT ?"
        params.append(page.limit + 1)
        _, rows = self._store.query(sql, params)
        if len(rows) > page.limit:
            last = rows[page.limit - 1][: len(order_columns)]
            next_token = encode_page_token(
                PageToken(cursor=last, limit=page.limit, filters=filters)
            )
            return rows[: page.limit], next_token
        return rows, None

    def projects(self) -> list[str]:
        _, rows = self._store.query(
            "SELECT DISTINCT project FROM sweeps ORDER BY project"
        )
        return [row[0] for row in rows]

    def sweeps(
        self,
        selection: Selection,
        *,
        states: tuple[str, ...] | None = None,
        page: Page | None = None,
        page_token: str | None = None,
    ) -> tuple[list[SweepRecord], str | None]:
        page = page or Page()
        sweep_ids = self._selected_sweep_ids(selection)
        if sweep_ids == []:
            return [], None
        clauses = ["s.project = ?"]
        params: list[Any] = [selection.project]
        if states:
            params.extend(states)
            clauses.append(f"s.state IN ({_placeholders(len(states))})")
        if sweep_ids is not None:
            params.extend(sweep_ids)
            clauses.append(f"s.sweep_id IN ({_placeholders(len(sweep_ids))})")
        rows, next_token = self._fetch_paged(
            sql=(
                "SELECT s.sweep_id, s.project, s.name, s.state FROM sweeps s "
                f"WHERE {' AND '.join(clauses)}"
            ),
            params=params,
            order_columns=("s.sweep_id",),
            page=page,
            page_token=page_token,
            filters=_echo(selection, states=states),
        )
        records = [
            SweepRecord(
                sweep_id=uuid.UUID(row[0]),
                project=row[1],
                name=row[2],
                state=row[3],
            )
            for row in rows
        ]
        return records, next_token

    def trials(
        self,
        selection: Selection,
        *,
        states: tuple[str, ...] | None = None,
        retry_roots_only: bool = False,
        page: Page | None = None,
        page_token: str | None = None,
    ) -> tuple[list[TrialRecord], str | None]:
        page = page or Page()
        where, params = self._trial_scope(selection)
        if states:
            params.extend(states)
            where += f" AND t.state IN ({_placeholders(len(states))})"
        if retry_roots_only:
            where += " AND t.retry_of_trial_id IS NULL"
        rows, next_token = self._fetch_paged(
            sql=(
                "SELECT t.sweep_id, t.number, t.trial_id, t.state, "
                "t.retry_of_trial_id, t.retry_root_trial_id, t.retry_index, "
                "t.objective, t.distributions_json, t.attrs_json "
                "FROM trials t JOIN sweeps s ON t.sweep_id = s.sweep_id "
                f"WHERE {where}"
            ),
            params=params,
            order_columns=("t.sweep_id", "t.number"),
            page=page,
            page_token=page_token,
            filters=_echo(selection, states=states, retry_roots_only=retry_roots_only),
        )
        records = [
            TrialRecord(
                trial_id=uuid.UUID(row[2]),
                sweep_id=uuid.UUID(row[0]),
                number=row[1],
                state=TrialState(row[3]),
                retry_of_trial_id=uuid.UUID(row[4]) if row[4] else None,
                retry_root_trial_id=uuid.UUID(row[5]),
                retry_index=row[6],
                objective=row[7],
                distributions=_flat(row[8]),
                attrs=_flat(row[9]),
            )
            for row in rows
        ]
        return records, next_token

    def trial_params(
        self,
        selection: Selection,
        *,
        kinds: tuple[str, ...] | None = None,
        page: Page | None = None,
        page_token: str | None = None,
    ) -> tuple[list[TrialParamRecord], str | None]:
        page = page or Page()
        where, params = self._trial_scope(selection)
        if kinds:
            params.extend(kinds)
            where += f" AND p.kind IN ({_placeholders(len(kinds))})"
        rows, next_token = self._fetch_paged(
            sql=(
                "SELECT p.trial_id, p.kind, p.key, p.value_json "
                "FROM trial_params p "
                "JOIN trials t ON p.trial_id = t.trial_id "
                "JOIN sweeps s ON t.sweep_id = s.sweep_id "
                f"WHERE {where}"
            ),
            params=params,
            order_columns=("p.trial_id", "p.kind", "p.key"),
            page=page,
            page_token=page_token,
            filters=_echo(selection, kinds=kinds),
        )
        records = [
            TrialParamRecord(
                trial_id=uuid.UUID(row[0]),
                kind=row[1],
                key=row[2],
                value=json.loads(row[3]),
            )
            for row in rows
        ]
        return records, next_token

    def lineage(self, selection: Selection) -> list[TrialLineageRecord]:
        where, params = self._trial_scope(selection)
        _, rows = self._store.query(
            "SELECT t.trial_id, t.retry_of_trial_id, t.retry_root_trial_id, "
            "t.retry_index, t.number, t.sweep_id "
            "FROM trials t JOIN sweeps s ON t.sweep_id = s.sweep_id "
            f"WHERE {where} ORDER BY t.retry_root_trial_id, t.retry_index",
            params,
        )
        return [
            TrialLineageRecord(
                trial_id=uuid.UUID(row[0]),
                retry_of_trial_id=uuid.UUID(row[1]) if row[1] else None,
                retry_root_trial_id=uuid.UUID(row[2]),
                retry_index=row[3],
                number=row[4],
                sweep_id=uuid.UUID(row[5]),
            )
            for row in rows
        ]

    def executions(
        self,
        selection: Selection,
        *,
        states: tuple[str, ...] | None = None,
        derive: bool = True,
        heartbeat_stale_s: float | None = None,
    ) -> list[ExecutionRecord]:
        where, params = self._trial_scope(selection)
        if selection.executions:
            params.extend(_ids(selection.executions))
            where += (
                f" AND e.execution_id IN ({_placeholders(len(selection.executions))})"
            )
        if states:
            bits = []
            if "running" in states:
                bits.append("e.ended_ns IS NULL")
            if "ended" in states:
                bits.append("e.ended_ns IS NOT NULL")
            if bits:
                where += f" AND ({' OR '.join(bits)})"
        _, rows = self._store.query(
            "SELECT e.execution_id, e.trial_id, e.hostname, e.started_ns, "
            "e.ended_ns, e.last_heartbeat_ns, e.last_observation_ns, "
            "e.outcome, e.exit_code, e.failure_kind "
            "FROM executions e "
            "JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN sweeps s ON t.sweep_id = s.sweep_id "
            f"WHERE {where} ORDER BY e.started_ns, e.execution_id",
            params,
        )
        threshold_s = (
            heartbeat_stale_s
            if heartbeat_stale_s is not None
            else self.heartbeat_stale_s
        )
        return [self._execution_record(row, derive, threshold_s) for row in rows]

    @staticmethod
    def _execution_record(
        row: tuple, derive: bool, heartbeat_stale_s: float
    ) -> ExecutionRecord:
        return ExecutionRecord(
            execution_id=uuid.UUID(row[0]),
            trial_id=uuid.UUID(row[1]),
            hostname=row[2],
            started_at=_from_ns(row[3]),
            ended_at=_from_ns(row[4]) if row[4] is not None else None,
            outcome=ExecutionOutcome(row[7]) if row[7] else None,
            exit_code=row[8],
            failure_kind=FailureKind(row[9]) if row[9] else None,
            last_heartbeat_ns=row[5],
            last_observation_ns=row[6],
            monitoring=(
                _monitoring(row[4], row[5], heartbeat_stale_s) if derive else None
            ),
        )

    def value_catalog(self, selection: Selection) -> list[ValueCatalogRecord]:
        where, params = self._trial_scope(selection)
        _, rows = self._store.query(
            "SELECT v.key, v.value_type, COUNT(*), MAX(v.step), "
            "COUNT(DISTINCT e.trial_id) "
            "FROM tracked_values v "
            "JOIN executions e ON v.execution_id = e.execution_id "
            "JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN sweeps s ON t.sweep_id = s.sweep_id "
            f"WHERE {where} GROUP BY v.key, v.value_type "
            "ORDER BY v.key, v.value_type",
            params,
        )
        return [
            ValueCatalogRecord(
                key=row[0],
                kind=row[1],
                n_points=row[2],
                latest_step=row[3],
                n_trials=row[4],
            )
            for row in rows
        ]

    def value_key_coverage(self, selection: Selection) -> list[dict[str, Any]]:
        """Per (key, kind) coverage: point count, distinct trials and
        retry families, and the step extent — the analysis picker's
        facts, with no semantics guessed from key names."""
        where, params = self._trial_scope(selection)
        _, rows = self._store.query(
            "SELECT v.key, v.value_type, COUNT(*), "
            "COUNT(DISTINCT e.trial_id), COUNT(DISTINCT t.retry_root_trial_id), "
            "MIN(v.step), MAX(v.step) "
            "FROM tracked_values v "
            "JOIN executions e ON v.execution_id = e.execution_id "
            "JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN sweeps s ON t.sweep_id = s.sweep_id "
            f"WHERE {where} GROUP BY v.key, v.value_type "
            "ORDER BY v.key, v.value_type",
            params,
        )
        return [
            {
                "key": row[0],
                "kind": row[1],
                "points": row[2],
                "trials": row[3],
                "families": row[4],
                "min_step": row[5],
                "max_step": row[6],
            }
            for row in rows
        ]

    def values(
        self,
        selection: Selection,
        *,
        keys: tuple[str, ...] | None = None,
        steps: tuple[int, ...] | None = None,
        since_ns: int | None = None,
        json_only: bool = False,
        page: Page | None = None,
        page_token: str | None = None,
    ) -> tuple[list[ValueRecord], str | None]:
        page = page or Page()
        where, params = self._value_where(selection, keys, steps, since_ns, json_only)
        return self._fetch_values(
            where,
            params,
            page,
            page_token,
            _echo(
                selection,
                keys=keys,
                steps=steps,
                since_ns=since_ns,
                json_only=json_only,
            ),
        )

    def value_series(
        self,
        selection: Selection,
        key: str,
        *,
        execution_ids: tuple[uuid.UUID, ...] | None = None,
        page: Page | None = None,
        page_token: str | None = None,
    ) -> tuple[list[ValueRecord], str | None]:
        page = page or Page()
        where, params = self._value_where(selection, (key,), None, None, False)
        if execution_ids:
            params.extend(_ids(execution_ids))
            where += f" AND v.execution_id IN ({_placeholders(len(execution_ids))})"
        return self._fetch_values(
            where,
            params,
            page,
            page_token,
            _echo(
                selection,
                key=key,
                execution_ids=_ids(execution_ids) if execution_ids else None,
            ),
        )

    def _value_where(
        self,
        selection: Selection,
        keys: tuple[str, ...] | None,
        steps: tuple[int, ...] | None,
        since_ns: int | None,
        json_only: bool,
    ) -> tuple[str, list[Any]]:
        where, params = self._trial_scope(selection)
        if selection.executions:
            params.extend(_ids(selection.executions))
            where += (
                f" AND v.execution_id IN ({_placeholders(len(selection.executions))})"
            )
        if keys:
            params.extend(keys)
            where += f" AND v.key IN ({_placeholders(len(keys))})"
        if steps:
            params.extend(steps)
            where += f" AND v.step IN ({_placeholders(len(steps))})"
        if since_ns is not None:
            params.append(since_ns)
            where += " AND v.recorded_ns >= ?"
        if json_only:
            where += " AND v.value_type = 'json'"
        return where, params

    def _fetch_values(
        self,
        where: str,
        params: list[Any],
        page: Page,
        page_token: str | None,
        filters: dict[str, Any],
    ) -> tuple[list[ValueRecord], str | None]:
        rows, next_token = self._fetch_paged(
            sql=(
                "SELECT v.execution_id, v.key, v.step, v.value_type, "
                "v.scalar_val, v.text_val, v.context, e.trial_id "
                "FROM tracked_values v "
                "JOIN executions e ON v.execution_id = e.execution_id "
                "JOIN trials t ON e.trial_id = t.trial_id "
                "JOIN sweeps s ON t.sweep_id = s.sweep_id "
                f"WHERE {where}"
            ),
            params=params,
            order_columns=("v.execution_id", "v.key", "v.step"),
            page=page,
            page_token=page_token,
            filters=filters,
        )
        return [self._value_record(row) for row in rows], next_token

    @staticmethod
    def _value_record(row: tuple) -> ValueRecord:
        value: ScalarValue = None
        observation: dict[str, Any] | None = None
        if row[3] == "scalar":
            value = row[4]
        else:
            payload = json.loads(row[5])
            if isinstance(payload, dict):
                observation = payload
            else:
                value = payload
        return ValueRecord(
            execution_id=uuid.UUID(row[0]),
            trial_id=uuid.UUID(row[7]),
            key=row[1],
            step=row[2],
            value=value,
            observation=observation,
            context=_flat(row[6]),
        )

    def artifacts(
        self,
        selection: Selection,
        *,
        keys: tuple[str, ...] | None = None,
        received: bool | None = None,
        source: ArtifactSource | None = None,
        page: Page | None = None,
        page_token: str | None = None,
    ) -> tuple[list[ArtifactRecord], str | None]:
        page = page or Page()
        where, params = self._trial_scope(selection)
        if keys:
            params.extend(keys)
            where += f" AND a.key IN ({_placeholders(len(keys))})"
        if received is True:
            where += " AND a.received_ns IS NOT NULL"
        elif received is False:
            where += " AND a.received_ns IS NULL"
        if source is not None:
            params.append(source)
            where += " AND a.source = ?"
        rows, next_token = self._fetch_paged(
            sql=(
                "SELECT a.artifact_id, a.trial_id, a.execution_id, a.key, "
                "a.filename, a.content_type, a.size_bytes, a.sha256, "
                "a.context_json, a.source, a.received_ns "
                "FROM artifacts a "
                "JOIN trials t ON a.trial_id = t.trial_id "
                "JOIN sweeps s ON t.sweep_id = s.sweep_id "
                f"WHERE {where}"
            ),
            params=params,
            order_columns=("a.artifact_id",),
            page=page,
            page_token=page_token,
            filters=_echo(selection, keys=keys, received=received, source=source),
        )
        records = [
            ArtifactRecord(
                artifact_id=uuid.UUID(row[0]),
                trial_id=uuid.UUID(row[1]),
                execution_id=uuid.UUID(row[2]) if row[2] else None,
                key=row[3],
                filename=row[4],
                content_type=row[5],
                size_bytes=row[6],
                sha256=row[7],
                context=_flat(row[8]),
                source=row[9],
                received_ns=row[10],
            )
            for row in rows
        ]
        return records, next_token

    @staticmethod
    def _artifact_row(row: tuple) -> dict[str, Any]:
        """One artifacts-table row as a dict, context JSON decoded."""
        return {
            "artifact_id": row[0],
            "execution_id": row[1],
            "key": row[2],
            "filename": row[3],
            "content_type": row[4],
            "size_bytes": row[5],
            "sha256": row[6],
            "context": json.loads(row[7]) if row[7] else None,
            "source": row[8],
            "declared_ns": row[9],
            "received_ns": row[10],
        }

    def trial_artifacts(self, trial_id: uuid.UUID) -> list[dict[str, Any]]:
        """Every artifact declared for one trial, ordered so repeated keys
        read as consecutive versions by declaration time."""
        _, rows = self._store.query(
            "SELECT a.artifact_id, a.execution_id, a.key, a.filename, "
            "a.content_type, a.size_bytes, a.sha256, a.context_json, "
            "a.source, a.declared_ns, a.received_ns "
            "FROM artifacts a WHERE a.trial_id = ? "
            "ORDER BY a.key, a.declared_ns, a.artifact_id",
            [str(trial_id)],
        )
        return [self._artifact_row(row) for row in rows]

    def artifact_context(self, artifact_id: uuid.UUID) -> dict[str, Any] | None:
        """Every stored fact for one artifact plus its trial/sweep context
        (deep-link entry point for the artifact viewer)."""
        _, rows = self._store.query(
            "SELECT a.artifact_id, a.execution_id, a.key, a.filename, "
            "a.content_type, a.size_bytes, a.sha256, a.context_json, "
            "a.source, a.declared_ns, a.received_ns, t.trial_id, "
            "s.sweep_id, s.project, s.name "
            "FROM artifacts a JOIN trials t ON a.trial_id = t.trial_id "
            "JOIN sweeps s ON t.sweep_id = s.sweep_id "
            "WHERE a.artifact_id = ?",
            [str(artifact_id)],
        )
        if not rows:
            return None
        row = rows[0]
        return {
            **self._artifact_row(row),
            "trial_id": row[11],
            "sweep_id": row[12],
            "project": row[13],
            "sweep_name": row[14],
        }

    def artifact_blob_path(self, artifact_id: uuid.UUID) -> Path | None:
        """Local path of the received blob file, or None when absent."""
        if self._artifacts_root is None:
            return None
        blob = self._store.artifact_blob(str(artifact_id))
        if blob is None:
            return None
        path = self._artifacts_root / blob[0]
        return path if path.is_file() else None

    def provenance(self, selection: Selection) -> list[ProvenanceRecord]:
        sweep_ids = self._selected_sweep_ids(selection)
        if sweep_ids == []:
            return []
        clauses = ["s.project = ?"]
        params: list[Any] = [selection.project]
        if sweep_ids is not None:
            params.extend(sweep_ids)
            clauses.append(f"sub.sweep_id IN ({_placeholders(len(sweep_ids))})")
        _, rows = self._store.query(
            "SELECT sub.submission_id, sub.sweep_id, sub.backend, "
            "sub.submitted_ns, sub.expected_trials, sub.git_hash, "
            "sub.config_source "
            "FROM submissions sub JOIN sweeps s ON sub.sweep_id = s.sweep_id "
            f"WHERE {' AND '.join(clauses)} ORDER BY sub.submission_id",
            params,
        )
        return [
            ProvenanceRecord(
                submission_id=uuid.UUID(row[0]),
                sweep_id=uuid.UUID(row[1]),
                backend=row[2],
                submitted_at_ns=row[3],
                expected_trials=row[4],
                git_hash=row[5],
                config_source=row[6],
            )
            for row in rows
        ]

    def job_resources(
        self,
        selection: Selection,
        *,
        job_ids: tuple[str, ...] | None = None,
        page: Page | None = None,
        page_token: str | None = None,
    ) -> tuple[list[JobResourceRecord], str | None]:
        """Captured sacct facts; explicit job ids bypass study scoping."""
        page = page or Page()
        cols = (
            "jr.job_id, jr.study_name, jr.submission_id, jr.wall_time_s, "
            "jr.cpu_time_s, jr.cpu_pct, jr.max_rss_mb, jr.ave_rss_mb, "
            "jr.alloc_cpus, jr.req_mem, jr.alloc_tres, jr.node_list, "
            "jr.state, jr.exit_code, jr.recorded_ns"
        )
        params: list[Any] = []
        if job_ids:
            sql = f"SELECT {cols} FROM job_resources jr "
            params.extend(job_ids)
            sql += f"WHERE jr.job_id IN ({_placeholders(len(job_ids))})"
        else:
            sweep_ids = self._selected_sweep_ids(selection)
            if sweep_ids == []:
                return [], None
            sql = f"SELECT {cols} FROM job_resources jr "
            sql += "JOIN sweeps s ON s.name = jr.study_name WHERE s.project = ?"
            params.append(selection.project)
            if sweep_ids is not None:
                params.extend(sweep_ids)
                sql += f" AND s.sweep_id IN ({_placeholders(len(sweep_ids))})"
        rows, next_token = self._fetch_paged(
            sql=sql,
            params=params,
            order_columns=("jr.job_id",),
            page=page,
            page_token=page_token,
            filters=_echo(selection, job_ids=job_ids),
        )
        return [
            JobResourceRecord(
                job_id=row[0],
                study_name=row[1],
                submission_id=row[2],
                wall_time_s=row[3],
                cpu_time_s=row[4],
                cpu_pct=row[5],
                max_rss_mb=row[6],
                ave_rss_mb=row[7],
                alloc_cpus=row[8],
                req_mem=row[9],
                alloc_tres=row[10],
                node_list=row[11],
                state=row[12],
                exit_code=row[13],
                recorded_at=_from_ns(row[14]),
            )
            for row in rows
        ], next_token

    def _monitoring_case(self) -> tuple[str, list[int]]:
        """SQL CASE + params mirroring :func:`_monitoring` exactly."""
        now_ns = time.time_ns()
        stale_ns = int(self.heartbeat_stale_s * 1_000_000_000)
        quiet_ns = int(stale_ns * _QUIET_FRACTION)
        sql = (
            "CASE WHEN e.ended_ns IS NOT NULL THEN 'ended' "
            "WHEN e.last_heartbeat_ns IS NULL THEN 'unknown' "
            "WHEN ? - e.last_heartbeat_ns <= ? THEN 'active' "
            "WHEN ? - e.last_heartbeat_ns <= ? THEN 'quiet' "
            "ELSE 'stale' END"
        )
        return sql, [now_ns, quiet_ns, now_ns, stale_ns]

    def project_catalog(self) -> list[dict[str, Any]]:
        """Per-project operational rollup over current sweeps: execution
        health counts, the most recently updated sweep, and last activity;
        archived and invalid sweep counts keep hidden history discoverable.

        Batched queries (no per-project loops); monitoring labels are
        derived in SQL with the same thresholds as :func:`_monitoring`.
        Projects stay listed even when every sweep is archived.
        """
        case_sql, case_params = self._monitoring_case()
        _, count_rows = self._store.query(
            f"WITH {_CURRENT_SWEEPS_CTES} "
            "SELECT ex.project, "
            "SUM(ex.label = 'active'), SUM(ex.label = 'quiet'), "
            "SUM(ex.label = 'stale'), SUM(ex.label = 'unknown'), "
            "SUM(ex.label = 'ended' AND ex.outcome = 'success'), "
            "SUM(ex.label = 'ended' AND ex.outcome = 'failure') "
            "FROM (SELECT cur.project AS project, e.outcome AS outcome, "
            f"{case_sql} AS label "
            "FROM executions e JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN current_sweeps cur ON cur.sweep_id = t.sweep_id) ex "
            "GROUP BY ex.project",
            case_params,
        )
        counts = {
            row[0]: dict(
                zip(
                    ("active", "quiet", "stale", "unknown", "succeeded", "failed"),
                    (value or 0 for value in row[1:]),
                    strict=True,
                )
            )
            for row in count_rows
        }
        _, activity_rows = self._store.query(
            f"WITH {_CURRENT_SWEEPS_CTES} "
            "SELECT project, MAX(updated_ns) FROM ("
            "SELECT project, updated_ns FROM current_sweeps "
            "UNION ALL SELECT cur.project, t.updated_ns FROM trials t "
            "JOIN current_sweeps cur ON cur.sweep_id = t.sweep_id "
            "UNION ALL SELECT cur.project, e.updated_ns FROM executions e "
            "JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN current_sweeps cur ON cur.sweep_id = t.sweep_id) "
            "GROUP BY project"
        )
        activity = {row[0]: row[1] for row in activity_rows}
        _, sweep_rows = self._store.query(
            f"WITH {_CURRENT_SWEEPS_CTES} "
            "SELECT cur.project, cur.name FROM current_sweeps cur "
            "JOIN (SELECT project, MAX(updated_ns) AS mx FROM current_sweeps "
            "GROUP BY project) m ON m.project = cur.project "
            "AND cur.updated_ns = m.mx ORDER BY cur.project, cur.name"
        )
        recent: dict[str, str] = {}
        for row in sweep_rows:
            recent.setdefault(row[0], row[1])
        _, curation_rows = self._store.query(
            f"WITH {_CURRENT_SWEEPS_CTES} "
            "SELECT project, SUM(archived), SUM(invalid) FROM sweep_curated "
            "GROUP BY project"
        )
        curation = {
            row[0]: {"archived_sweeps": row[1] or 0, "invalid_sweeps": row[2] or 0}
            for row in curation_rows
        }
        return [
            {
                "project": project,
                **(_ZERO_COUNTS | counts.get(project, {})),
                "recent_sweep": recent.get(project),
                "last_activity_ns": activity.get(project),
                **curation[project],
            }
            for project in sorted(curation)
        ]

    def sweep_overview(self, selection: Selection) -> list[dict[str, Any]]:
        """Per-sweep operational rollup: submission/job facts, execution
        monitoring distribution, and trial liveness counts in one query."""
        sweep_ids = self._selected_sweep_ids(selection)
        if sweep_ids == []:
            return []
        scope = "s.project = ?"
        params: list[Any] = [selection.project]
        if sweep_ids is not None:
            params.extend(sweep_ids)
            scope += f" AND s.sweep_id IN ({_placeholders(len(sweep_ids))})"
        case_sql, case_params = self._monitoring_case()
        _, rows = self._store.query(
            "WITH sel AS ("
            "SELECT s.sweep_id AS sweep_id, s.name AS name, s.state AS state, "
            "s.updated_ns AS updated_ns, c.archived_ns AS archived_ns, "
            "c.invalid_ns AS invalid_ns, c.invalid_reason AS invalid_reason "
            "FROM sweeps s LEFT JOIN sweep_curation c ON c.sweep_id = s.sweep_id "
            f"WHERE {scope}), "
            "latest_sub AS ("
            "SELECT sweep_id, submitted_ns, backend, expected_trials FROM ("
            "SELECT sub.sweep_id AS sweep_id, sub.submitted_ns AS submitted_ns, "
            "sub.backend AS backend, sub.expected_trials AS expected_trials, "
            "ROW_NUMBER() OVER (PARTITION BY sub.sweep_id ORDER BY "
            "COALESCE(sub.submitted_ns, sub.created_ns) DESC, "
            "sub.submission_id) AS rn "
            "FROM submissions sub JOIN sel ON sub.sweep_id = sel.sweep_id) "
            "WHERE rn = 1), "
            "jobs AS ("
            "SELECT sub.sweep_id AS sweep_id, COUNT(*) AS n_jobs "
            "FROM submission_jobs j JOIN submissions sub "
            "ON j.submission_id = sub.submission_id "
            "JOIN sel ON sub.sweep_id = sel.sweep_id GROUP BY sub.sweep_id), "
            "mon AS ("
            "SELECT x.sweep_id AS sweep_id, COUNT(*) AS started, "
            "SUM(x.label = 'ended') AS terminal, "
            "SUM(x.label = 'active') AS n_active, "
            "SUM(x.label = 'quiet') AS n_quiet, "
            "SUM(x.label = 'stale') AS n_stale, "
            "SUM(x.label = 'unknown') AS n_unknown, "
            "SUM(x.label = 'ended' AND x.outcome = 'success') AS n_succeeded, "
            "SUM(x.label = 'ended' AND x.outcome = 'failure') AS n_failed "
            "FROM (SELECT t.sweep_id AS sweep_id, e.outcome AS outcome, "
            f"{case_sql} AS label "
            "FROM executions e JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN sel ON t.sweep_id = sel.sweep_id) x GROUP BY x.sweep_id), "
            "trial_states AS ("
            "SELECT t.sweep_id AS sweep_id, "
            "COUNT(*) AS trials, "
            "SUM(t.state = 'completed') AS trials_complete, "
            "MIN(CASE WHEN t.state = 'completed' THEN t.objective END) "
            "AS best_objective, "
            "SUM(t.state = 'waiting') AS waiting, "
            "SUM(t.state = 'running') AS running "
            "FROM trials t JOIN sel ON t.sweep_id = sel.sweep_id "
            "GROUP BY t.sweep_id) "
            "SELECT sel.sweep_id, sel.name, sel.state, ls.submitted_ns, "
            "ls.backend, ls.expected_trials, COALESCE(j.n_jobs, 0), "
            "COALESCE(m.started, 0), COALESCE(m.terminal, 0), "
            "COALESCE(m.n_active, 0), COALESCE(m.n_quiet, 0), "
            "COALESCE(m.n_stale, 0), COALESCE(m.n_unknown, 0), "
            "COALESCE(m.n_succeeded, 0), COALESCE(m.n_failed, 0), "
            "COALESCE(ts.waiting, 0), COALESCE(ts.running, 0), "
            "COALESCE(ts.trials, 0), COALESCE(ts.trials_complete, 0), "
            "ts.best_objective, "
            "sel.archived_ns, sel.invalid_ns, sel.invalid_reason "
            "FROM sel LEFT JOIN latest_sub ls ON ls.sweep_id = sel.sweep_id "
            "LEFT JOIN jobs j ON j.sweep_id = sel.sweep_id "
            "LEFT JOIN mon m ON m.sweep_id = sel.sweep_id "
            "LEFT JOIN trial_states ts ON ts.sweep_id = sel.sweep_id "
            "ORDER BY sel.updated_ns DESC, sel.sweep_id",
            [*params, *case_params],
        )
        return [
            dict(
                zip(
                    (
                        "sweep_id",
                        "name",
                        "state",
                        "latest_submitted_ns",
                        "backend",
                        "expected_trials",
                        "submitted_jobs",
                        "started",
                        "terminal",
                        "active",
                        "quiet",
                        "stale",
                        "unknown",
                        "succeeded",
                        "failed",
                        "waiting_trials",
                        "running_trials",
                        "trials",
                        "trials_complete",
                        "best_objective",
                        "archived_ns",
                        "invalid_ns",
                        "invalid_reason",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in rows
        ]

    def failed_executions(
        self,
        selection: Selection,
        *,
        limit: int = 200,
        include_curated: bool = False,
    ) -> list[dict[str, Any]]:
        """Failed executions under the selection, most recent first;
        sweeps hidden by curation stay out so the list matches the
        overview roll-up's failed counts, unless ``include_curated``
        pulls the project's historical list."""
        sweep_ids = self._selected_sweep_ids(selection)
        if sweep_ids == []:
            return []
        scope = "cur.project = ?"
        params: list[Any] = [selection.project]
        if sweep_ids is not None:
            params.extend(sweep_ids)
            scope += f" AND cur.sweep_id IN ({_placeholders(len(sweep_ids))})"
        params.append(limit)
        _, rows = self._store.query(
            f"WITH {_ALL_SWEEPS_CTES if include_curated else _CURRENT_SWEEPS_CTES} "
            "SELECT cur.sweep_id, cur.name, t.trial_id, t.number, "
            "e.execution_id, e.failure_kind, e.failure_summary, e.exit_code, "
            "e.hostname, e.updated_ns "
            "FROM executions e "
            "JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN current_sweeps cur ON cur.sweep_id = t.sweep_id "
            f"WHERE {scope} AND e.outcome = 'failure' "
            "ORDER BY e.updated_ns DESC, e.execution_id LIMIT ?",
            params,
        )
        return [
            dict(
                zip(
                    (
                        "sweep_id",
                        "sweep_name",
                        "trial_id",
                        "trial_number",
                        "execution_id",
                        "failure_kind",
                        "failure_summary",
                        "exit_code",
                        "hostname",
                        "updated_ns",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in rows
        ]

    def submission_jobs(self, selection: Selection) -> list[dict[str, Any]]:
        """Submission rows with their scheduler jobs attached (LEFT JOIN so
        job-less submissions stay visible); one dict per job-or-submission."""
        sweep_ids = self._selected_sweep_ids(selection)
        if sweep_ids == []:
            return []
        clauses = ["s.project = ?"]
        params: list[Any] = [selection.project]
        if sweep_ids is not None:
            params.extend(sweep_ids)
            clauses.append(f"sub.sweep_id IN ({_placeholders(len(sweep_ids))})")
        _, rows = self._store.query(
            "SELECT sub.submission_id, sub.backend, sub.state, "
            "sub.submitted_ns, sub.expected_trials, j.job_id, "
            "j.scheduler_job_id, j.role, j.state "
            "FROM submissions sub JOIN sweeps s ON sub.sweep_id = s.sweep_id "
            "LEFT JOIN submission_jobs j ON j.submission_id = sub.submission_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY COALESCE(sub.submitted_ns, sub.created_ns), "
            "sub.submission_id, j.scheduler_job_id",
            params,
        )
        return [
            dict(
                zip(
                    (
                        "submission_id",
                        "backend",
                        "submission_state",
                        "submitted_ns",
                        "expected_trials",
                        "job_id",
                        "scheduler_job_id",
                        "role",
                        "job_state",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in rows
        ]

    def execution_progress(self, selection: Selection) -> list[dict[str, Any]]:
        """Explicit progress rows (current/total/unit) for the selection's
        executions, with ended_ns so callers can keep in-flight rows only."""
        where, params = self._trial_scope(selection)
        _, rows = self._store.query(
            "SELECT p.execution_id, p.current, p.total, p.unit, e.ended_ns "
            "FROM execution_progress p "
            "JOIN executions e ON p.execution_id = e.execution_id "
            "JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN sweeps s ON t.sweep_id = s.sweep_id "
            f"WHERE {where} ORDER BY p.execution_id",
            params,
        )
        return [
            {
                "execution_id": row[0],
                "current": row[1],
                "total": row[2],
                "unit": row[3],
                "ended_ns": row[4],
            }
            for row in rows
        ]

    def trial_families(self, selection: Selection) -> list[dict[str, Any]]:
        """One row per retry family (root trial): the current (latest
        generation) trial with its state/objective, generation count,
        and the current trial's execution short ids."""
        where, params = self._trial_scope(selection)
        _, rows = self._store.query(
            "SELECT f.trial_id, f.retry_root_trial_id, f.retry_index, f.state, "
            "f.objective, f.number, f.generations, COALESCE(("
            "SELECT group_concat(x, ', ') FROM ("
            "SELECT substr(e.execution_id, 1, 8) AS x FROM executions e "
            "WHERE e.trial_id = f.trial_id ORDER BY e.execution_id)), '') "
            "FROM ("
            "SELECT t.trial_id AS trial_id, "
            "t.retry_root_trial_id AS retry_root_trial_id, "
            "t.retry_index AS retry_index, t.state AS state, "
            "t.objective AS objective, t.number AS number, "
            "ROW_NUMBER() OVER (PARTITION BY t.retry_root_trial_id "
            "ORDER BY t.retry_index DESC, t.trial_id) AS rn, "
            "COUNT(*) OVER (PARTITION BY t.retry_root_trial_id) AS generations "
            "FROM trials t JOIN sweeps s ON t.sweep_id = s.sweep_id "
            f"WHERE {where}) AS f WHERE f.rn = 1 ORDER BY f.retry_root_trial_id",
            params,
        )
        return [
            {
                "root": row[1],
                "current_trial": row[0],
                "retry_index": row[2],
                "state": row[3],
                "objective": row[4],
                "number": row[5],
                "generations": row[6],
                "executions": row[7],
            }
            for row in rows
        ]

    def context_catalog(self, selection: Selection) -> list[dict[str, Any]]:
        """Flat context dimensions across the selection's tracked values.

        One row per context key from a single DISTINCT ``json_each`` scan:
        every distinct formatted value (the filter options), the
        cardinality, and up to five samples — no key is special-cased
        and no paginated values read is followed.
        """
        where, params = self._trial_scope(selection)
        _, rows = self._store.query(
            "SELECT DISTINCT je.key, je.type, CAST(je.value AS TEXT) "
            "FROM tracked_values v "
            "JOIN executions e ON v.execution_id = e.execution_id "
            "JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN sweeps s ON t.sweep_id = s.sweep_id "
            "CROSS JOIN json_each(v.context) je "
            f"WHERE {where} ORDER BY je.key, 3",
            params,
        )
        grouped: dict[str, list[str]] = {}
        for key, json_type, value in rows:
            if json_type in ("true", "false"):
                value = json_type
            grouped.setdefault(key, []).append(value)
        return [
            {
                "key": key,
                "values": values,
                "cardinality": len(values),
                "samples": values[:_CONTEXT_SAMPLE_LIMIT],
            }
            for key, values in sorted(grouped.items())
        ]

    def trial_numbers_objectives(self, selection: Selection) -> list[dict[str, Any]]:
        """Optimizer-neutral trial rows for study-style figures: number,
        state, objective, retry identity, timestamps, and flat params in
        one batched query."""
        where, params = self._trial_scope(selection)
        _, rows = self._store.query(
            "SELECT t.trial_id, t.sweep_id, t.number, t.state, t.objective, "
            "t.created_ns, t.updated_ns, t.retry_index, "
            "COALESCE((SELECT json_group_object(p.key, json(p.value_json)) "
            "FROM trial_params p WHERE p.trial_id = t.trial_id), '{}') "
            "FROM trials t JOIN sweeps s ON t.sweep_id = s.sweep_id "
            f"WHERE {where} ORDER BY t.sweep_id, t.number, t.retry_index",
            params,
        )
        return [
            {
                "trial_id": row[0],
                "sweep_id": row[1],
                "number": row[2],
                "state": row[3],
                "objective": row[4],
                "created_ns": row[5],
                "updated_ns": row[6],
                "retry_index": row[7],
                "params": json.loads(row[8]),
            }
            for row in rows
        ]

    def sweep_context(self, sweep_id: uuid.UUID) -> dict[str, Any] | None:
        """Identity facts for one sweep (deep-link entry point)."""
        _, rows = self._store.query(
            "SELECT sweep_id, project, name, state FROM sweeps WHERE sweep_id = ?",
            [str(sweep_id)],
        )
        if not rows:
            return None
        sweep_id_text, project, name, state = rows[0]
        return {
            "sweep_id": sweep_id_text,
            "project": project,
            "name": name,
            "state": state,
        }

    def trial_context(self, trial_id: uuid.UUID) -> dict[str, Any] | None:
        """Identity + lineage facts for one trial (deep-link entry point)."""
        _, rows = self._store.query(
            "SELECT t.trial_id, t.sweep_id, t.number, t.state, t.objective, "
            "t.retry_of_trial_id, t.retry_root_trial_id, t.retry_index, "
            "s.project, s.name, s.state "
            "FROM trials t JOIN sweeps s ON t.sweep_id = s.sweep_id "
            "WHERE t.trial_id = ?",
            [str(trial_id)],
        )
        if not rows:
            return None
        keys = (
            "trial_id",
            "sweep_id",
            "number",
            "state",
            "objective",
            "retry_of_trial_id",
            "retry_root_trial_id",
            "retry_index",
            "project",
            "sweep_name",
            "sweep_state",
        )
        return dict(zip(keys, rows[0], strict=True))

    def execution_context(self, execution_id: uuid.UUID) -> dict[str, Any] | None:
        """Every stored fact for one execution plus its trial/sweep context,
        the derived monitoring label, and its explicit progress row."""
        _, rows = self._store.query(
            "SELECT e.execution_id, e.trial_id, e.hostname, e.started_ns, "
            "e.ended_ns, e.last_heartbeat_ns, e.last_observation_ns, "
            "e.outcome, e.exit_code, e.failure_kind, e.failure_summary, "
            "t.sweep_id, t.number, t.state, t.objective, "
            "t.retry_of_trial_id, t.retry_root_trial_id, t.retry_index, "
            "s.project, s.name, s.state "
            "FROM executions e JOIN trials t ON e.trial_id = t.trial_id "
            "JOIN sweeps s ON t.sweep_id = s.sweep_id "
            "WHERE e.execution_id = ?",
            [str(execution_id)],
        )
        if not rows:
            return None
        keys = (
            "execution_id",
            "trial_id",
            "hostname",
            "started_ns",
            "ended_ns",
            "last_heartbeat_ns",
            "last_observation_ns",
            "outcome",
            "exit_code",
            "failure_kind",
            "failure_summary",
            "sweep_id",
            "number",
            "trial_state",
            "objective",
            "retry_of_trial_id",
            "retry_root_trial_id",
            "retry_index",
            "project",
            "sweep_name",
            "sweep_state",
        )
        row = rows[0]
        context = dict(zip(keys, row, strict=True))
        context["monitoring"] = _monitoring(row[4], row[5], self.heartbeat_stale_s)
        _, progress_rows = self._store.query(
            "SELECT current, total, unit FROM execution_progress "
            "WHERE execution_id = ?",
            [str(execution_id)],
        )
        context["progress"] = (
            {
                "current": progress_rows[0][0],
                "total": progress_rows[0][1],
                "unit": progress_rows[0][2],
            }
            if progress_rows
            else None
        )
        return context
