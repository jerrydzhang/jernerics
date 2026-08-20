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
from typing import Any, Literal

from jernerics_schema import (
    ArtifactRecord,
    ArtifactSource,
    ExecutionOutcome,
    ExecutionRecord,
    FailureKind,
    FlatContext,
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


class QueryService:
    """Every domain read over the v3 store, shared by HTTP and callbacks."""

    def __init__(self, store: Store, *, heartbeat_stale_s: float = 900.0) -> None:
        self._store = store
        self.heartbeat_stale_s = heartbeat_stale_s

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
