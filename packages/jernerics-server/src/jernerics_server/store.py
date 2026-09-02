"""Normalized v3 SQLite tracking store.

Canonical schema for the tracking server: sweeps own submissions (with
scheduler jobs) and trials; trials own executions; executions own values,
progress, and artifacts. The store refuses to open legacy (v1/v2)
databases; operators archive them with :func:`archive_v2` and start v3 on
a fresh path. Future schema versions are applied by the ordered migration
machinery in ``_MIGRATIONS`` and never drop tracking data.
"""

import hashlib
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Self

from jernerics_schema import (
    ExecutionOutcome,
    FailureKind,
    SubmissionState,
    TrialState,
)

SCHEMA_VERSION = 9

_V2_TABLES = ("sweep_meta", "trial_end", "params")

_ARCHIVE_MANIFEST = "SHA256SUMS"


class StoreError(Exception):
    """Base class for store failures."""


class LegacyStoreError(StoreError):
    """A v1/v2 tracking database is present; automatic startup is refused."""


class FutureSchemaError(StoreError):
    """The database schema version is newer than this build supports."""


class QueryResourceLimitError(StoreError):
    """A raw query exceeded its VM-step or wall-clock budget."""


class SweepNotFoundError(StoreError):
    """A curation mutation named a sweep id no sweep row carries."""


class SweepStillInvalidError(StoreError):
    """Unarchiving was requested while the sweep remains invalid."""


class InvalidCurationReasonError(StoreError):
    """The invalid-marking reason is blank or longer than 500 characters."""


class InvestigationNotFoundError(StoreError):
    """An investigation lookup or mutation named an unknown investigation."""


class InvestigationConflictError(StoreError):
    """A create named an existing (project, name) with a different body."""


class CrossProjectSweepError(StoreError):
    """An investigation membership change named a sweep from another project."""


_INVESTIGATION_COLUMNS = (
    "investigation_id",
    "project",
    "name",
    "factor",
    "outcome",
    "replicate_factor",
    "archived_ns",
    "created_ns",
    "updated_ns",
)


class QueryNotAuthorizedError(StoreError):
    """The read-only query authorizer rejected a statement."""


MAX_QUERY_VM_STEPS = 50_000_000
"""VM-step budget for one raw SQL execution."""

MAX_QUERY_SECONDS = 5.0
"""Wall-clock budget for one raw SQL execution."""

_PROGRESS_PERIOD = 1_000_000
"""VM steps between raw-query resource checks."""

_WRITE_ACTION_NAMES = (
    "SQLITE_DELETE",
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_ALTER_TABLE",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_ATTACH",
    "SQLITE_DETACH",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
)

_WRITE_ACTIONS = frozenset(
    getattr(sqlite3, name) for name in _WRITE_ACTION_NAMES if hasattr(sqlite3, name)
)


def _read_only_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    if action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _enum_check(column: str, enum: type[Enum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"CHECK({column} IN ({values}))"


_TABLE_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE sweeps (
        sweep_id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        name TEXT NOT NULL,
        state TEXT NOT NULL,
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        UNIQUE(project, name)
    ) STRICT
    """,
    f"""
    CREATE TABLE submissions (
        submission_id TEXT PRIMARY KEY,
        sweep_id TEXT NOT NULL REFERENCES sweeps(sweep_id),
        backend TEXT NOT NULL,
        state TEXT NOT NULL {_enum_check("state", SubmissionState)},
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) STRICT
    """,
    f"""
    CREATE TABLE submission_jobs (
        job_id TEXT PRIMARY KEY,
        submission_id TEXT NOT NULL REFERENCES submissions(submission_id),
        scheduler_job_id TEXT NOT NULL,
        state TEXT NOT NULL {_enum_check("state", SubmissionState)},
        updated_ns INTEGER NOT NULL,
        UNIQUE(submission_id, scheduler_job_id)
    ) STRICT
    """,
    f"""
    CREATE TABLE trials (
        trial_id TEXT PRIMARY KEY,
        sweep_id TEXT NOT NULL REFERENCES sweeps(sweep_id),
        number INTEGER NOT NULL,
        state TEXT NOT NULL {_enum_check("state", TrialState)},
        retry_of_trial_id TEXT REFERENCES trials(trial_id),
        retry_root_trial_id TEXT NOT NULL REFERENCES trials(trial_id),
        retry_index INTEGER NOT NULL CHECK(retry_index >= 0),
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL,
        UNIQUE(sweep_id, number)
    ) STRICT
    """,
    """
    CREATE TABLE trial_params (
        trial_id TEXT NOT NULL REFERENCES trials(trial_id),
        kind TEXT NOT NULL CHECK(kind IN ('sampled', 'manual')),
        key TEXT NOT NULL,
        value_json TEXT NOT NULL CHECK(json_valid(value_json)),
        updated_ns INTEGER NOT NULL,
        PRIMARY KEY(trial_id, kind, key)
    ) STRICT
    """,
    f"""
    CREATE TABLE executions (
        execution_id TEXT PRIMARY KEY,
        trial_id TEXT NOT NULL REFERENCES trials(trial_id),
        hostname TEXT NOT NULL,
        started_ns INTEGER NOT NULL,
        ended_ns INTEGER,
        last_heartbeat_ns INTEGER,
        last_observation_ns INTEGER,
        outcome TEXT {_enum_check("outcome", ExecutionOutcome)},
        exit_code INTEGER,
        failure_kind TEXT {_enum_check("failure_kind", FailureKind)},
        failure_summary TEXT
            CHECK(failure_summary IS NULL OR length(failure_summary) <= 2000),
        created_ns INTEGER NOT NULL,
        updated_ns INTEGER NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE execution_progress (
        execution_id TEXT PRIMARY KEY REFERENCES executions(execution_id),
        current INTEGER NOT NULL CHECK(current >= 0),
        total INTEGER NOT NULL CHECK(total > 0),
        unit TEXT NOT NULL,
        updated_ns INTEGER NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE tracked_values (
        execution_id TEXT NOT NULL REFERENCES executions(execution_id),
        key TEXT NOT NULL,
        step INTEGER NOT NULL CHECK(step >= 0),
        value_type TEXT NOT NULL CHECK(
            (value_type = 'scalar' AND text_val IS NULL AND scalar_val IS NOT NULL)
            OR (value_type = 'json' AND text_val IS NOT NULL AND scalar_val IS NULL)
        ),
        scalar_val REAL,
        text_val TEXT,
        context TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(context)),
        recorded_ns INTEGER NOT NULL,
        PRIMARY KEY(execution_id, key, step)
    ) STRICT
    """,
    """
    CREATE TABLE artifacts (
        artifact_id TEXT PRIMARY KEY,
        trial_id TEXT NOT NULL REFERENCES trials(trial_id),
        execution_id TEXT REFERENCES executions(execution_id),
        key TEXT NOT NULL,
        filename TEXT NOT NULL,
        content_type TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
        sha256 TEXT CHECK(sha256 IS NULL OR length(sha256) = 64),
        declared_ns INTEGER NOT NULL,
        received_ns INTEGER
    ) STRICT
    """,
    """
    CREATE TABLE artifact_blobs (
        artifact_id TEXT PRIMARY KEY REFERENCES artifacts(artifact_id),
        rel_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        received_ns INTEGER NOT NULL
    ) STRICT
    """,
    """
    CREATE TABLE reconciliation_conflicts (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        trial_id TEXT NOT NULL REFERENCES trials(trial_id),
        kind TEXT NOT NULL,
        detail TEXT NOT NULL CHECK(json_valid(detail)),
        detected_ns INTEGER NOT NULL
    ) STRICT
    """,
)

_INDEX_STATEMENTS: tuple[str, ...] = (
    "CREATE INDEX idx_sweeps_recency ON sweeps(updated_ns)",
    "CREATE INDEX idx_trials_state ON trials(state)",
    "CREATE INDEX idx_trials_retry_root ON trials(retry_root_trial_id)",
    "CREATE INDEX idx_executions_heartbeat ON executions(last_heartbeat_ns)",
    "CREATE INDEX idx_executions_outcome ON executions(outcome)",
    "CREATE INDEX idx_artifacts_exec_key ON artifacts(execution_id, key)",
    "CREATE INDEX idx_executions_trial ON executions(trial_id)",
    "CREATE INDEX idx_submissions_sweep ON submissions(sweep_id)",
)


def _migrate_to_v3(con: sqlite3.Connection) -> None:
    for statement in (*_TABLE_STATEMENTS, *_INDEX_STATEMENTS):
        con.execute(statement)


def _migrate_to_v4(con: sqlite3.Connection) -> None:
    for statement in (
        "ALTER TABLE submissions ADD COLUMN submitted_ns INTEGER",
        "ALTER TABLE submissions ADD COLUMN expected_trials INTEGER",
        "ALTER TABLE submissions ADD COLUMN git_hash TEXT",
        "ALTER TABLE submissions ADD COLUMN config_source TEXT",
        "ALTER TABLE submission_jobs ADD COLUMN role TEXT",
    ):
        con.execute(statement)


def _migrate_to_v5(con: sqlite3.Connection) -> None:
    for statement in (
        "ALTER TABLE trials ADD COLUMN objective REAL",
        "ALTER TABLE trials ADD COLUMN distributions_json TEXT",
        "ALTER TABLE trials ADD COLUMN attrs_json TEXT",
    ):
        con.execute(statement)


def _migrate_to_v6(con: sqlite3.Connection) -> None:
    for statement in (
        "ALTER TABLE artifacts ADD COLUMN context_json TEXT",
        "ALTER TABLE artifacts ADD COLUMN source TEXT NOT NULL DEFAULT 'user'",
    ):
        con.execute(statement)


def _migrate_to_v7(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE sweep_curation (
            sweep_id TEXT PRIMARY KEY REFERENCES sweeps(sweep_id),
            archived_ns INTEGER,
            invalid_ns INTEGER,
            invalid_reason TEXT CHECK(
                invalid_reason IS NULL
                OR (length(trim(invalid_reason)) > 0
                    AND length(trim(invalid_reason)) <= 500)
            ),
            updated_ns INTEGER NOT NULL,
            CHECK(
                (invalid_ns IS NULL AND invalid_reason IS NULL)
                OR (
                    invalid_ns IS NOT NULL
                    AND invalid_reason IS NOT NULL
                    AND archived_ns IS NOT NULL
                )
            )
        ) STRICT
        """
    )


def _migrate_to_v8(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE job_resources (
            event_id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            study_name TEXT,
            submission_id TEXT,
            wall_time_s REAL,
            cpu_time_s REAL,
            cpu_pct REAL,
            max_rss_mb REAL,
            ave_rss_mb REAL,
            alloc_cpus INTEGER,
            req_mem TEXT,
            alloc_tres TEXT,
            node_list TEXT,
            state TEXT,
            exit_code TEXT,
            recorded_ns INTEGER NOT NULL
        ) STRICT
        """
    )
    con.execute("CREATE INDEX idx_job_resources_job ON job_resources(job_id)")


def _migrate_to_v9(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE investigations (
            investigation_id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            name TEXT NOT NULL,
            factor TEXT NOT NULL,
            outcome TEXT NOT NULL,
            replicate_factor TEXT NULL,
            archived_ns INTEGER NULL,
            created_ns INTEGER NOT NULL,
            updated_ns INTEGER NOT NULL,
            UNIQUE(project, name)
        ) STRICT
        """
    )
    con.execute(
        """
        CREATE TABLE investigation_sweeps (
            investigation_id TEXT NOT NULL
                REFERENCES investigations(investigation_id),
            sweep_id TEXT NOT NULL REFERENCES sweeps(sweep_id),
            added_ns INTEGER NOT NULL,
            PRIMARY KEY(investigation_id, sweep_id)
        ) STRICT
        """
    )
    con.execute(
        "CREATE INDEX idx_investigation_sweeps_sweep ON investigation_sweeps(sweep_id)"
    )


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
    6: _migrate_to_v6,
    7: _migrate_to_v7,
    8: _migrate_to_v8,
    9: _migrate_to_v9,
}


def _legacy_error(path: Path, detail: str) -> LegacyStoreError:
    return LegacyStoreError(
        f"Refusing to open {path}: {detail}. This is a legacy (v1/v2) "
        "tracking database; the v3 store never migrates it in place and "
        "never drops tracking data. To cut over:\n"
        "  1. Stop any server still using this database.\n"
        "  2. Archive the database and its artifact root with the archive "
        "helper archive_v2:\n"
        '     python -c "from jernerics_server.store import archive_v2; '
        "archive_v2('<db-path>', '<artifacts-root>', '<archive-dir>')\"\n"
        "  3. Move the archived database file (and the artifact root) aside "
        "so the store path is fresh.\n"
        "  4. Start the v3 server again; it creates a new empty v3 store on "
        "the fresh path.\n"
        "The archive lands in a timestamped directory carrying a SHA256SUMS "
        "manifest of every archived file."
    )


class Store:
    """SQLite tracking store for schema v3."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(
            str(self._path), check_same_thread=False, isolation_level=None
        )
        try:
            row = self._con.execute("PRAGMA user_version").fetchone()
            version = row[0] if row else 0
            self._refuse_legacy(version)
            if version > SCHEMA_VERSION:
                raise FutureSchemaError(
                    f"{self._path} uses schema version {version}, newer than "
                    f"the supported version {SCHEMA_VERSION}; upgrade "
                    "jernerics-server to open it."
                )
            self._configure(busy_timeout_ms)
            if version < SCHEMA_VERSION:
                self._apply_migrations(version)
        except BaseException:
            self._con.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._con.close()

    @property
    def path(self) -> Path:
        """Database file location (the dashboard secret lives beside it)."""
        return self._path

    def _refuse_legacy(self, version: int) -> None:
        placeholders = ", ".join("?" * len(_V2_TABLES))
        present = [
            row[0]
            for row in self._con.execute(
                "SELECT name FROM sqlite_master "
                f"WHERE type = 'table' AND name IN ({placeholders})",
                _V2_TABLES,
            )
        ]
        if version in (1, 2):
            detail = f"user_version is {version}"
        elif present:
            detail = f"legacy tables present: {', '.join(sorted(present))}"
        else:
            return
        raise _legacy_error(self._path, detail)

    def _configure(self, busy_timeout_ms: int) -> None:
        # Wait (up to busy_timeout_ms) instead of failing immediately when
        # another connection holds a lock; also applies to the WAL switch.
        self._con.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        # WAL keeps read-only query connections unblocked while the single
        # writer commits; the mode persists in the database header.
        self._con.execute("PRAGMA journal_mode=WAL")
        # NORMAL is safe with WAL: a power loss may lose the most recent
        # commits but cannot corrupt the store. synchronous=FULL is
        # available for extra durability at throughput cost.
        self._con.execute("PRAGMA synchronous=NORMAL")
        # Enforce the declared REFERENCES constraints on every write.
        self._con.execute("PRAGMA foreign_keys=ON")
        # 1000 pages is SQLite's default WAL autocheckpoint rate; stated
        # explicitly so WAL growth policy is part of the schema contract.
        self._con.execute("PRAGMA wal_autocheckpoint=1000")

    def _apply_migrations(self, current: int) -> None:
        targets = [target for target in sorted(_MIGRATIONS) if target > current]
        if not targets:
            raise StoreError(f"no migration path from schema version {current}")
        self._con.execute("BEGIN IMMEDIATE")
        try:
            for target in targets:
                _MIGRATIONS[target](self._con)
                self._con.execute(f"PRAGMA user_version={target}")
            self._con.execute("COMMIT")
        except BaseException:
            self._con.execute("ROLLBACK")
            raise

    def query(
        self, sql: str, params: list | None = None
    ) -> tuple[list[str], list[tuple]]:
        con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            deadline = time.monotonic() + MAX_QUERY_SECONDS
            checks = 0

            def _over_budget() -> int:
                nonlocal checks
                checks += 1
                return (
                    1
                    if checks * _PROGRESS_PERIOD > MAX_QUERY_VM_STEPS
                    or time.monotonic() > deadline
                    else 0
                )

            con.set_progress_handler(_over_budget, _PROGRESS_PERIOD)
            con.set_authorizer(_read_only_authorizer)
            cursor = con.execute(sql, params or [])
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return columns, rows
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise QueryResourceLimitError(
                    f"query exceeded resource limits ({MAX_QUERY_VM_STEPS} VM "
                    f"steps / {MAX_QUERY_SECONDS}s wall clock)"
                ) from e
            raise
        except sqlite3.DatabaseError as e:
            if "not authorized" in str(e):
                raise QueryNotAuthorizedError(str(e)) from e
            raise
        finally:
            con.close()

    def artifact_declaration(self, artifact_id: str) -> tuple | None:
        """(filename, content_type, sha256, size_bytes, received_ns) or None."""
        with self._lock:
            return self._con.execute(
                "SELECT filename, content_type, sha256, size_bytes, received_ns "
                "FROM artifacts WHERE artifact_id = ?",
                [artifact_id],
            ).fetchone()

    def artifact_blob(self, artifact_id: str) -> tuple | None:
        """(rel_path, sha256, size_bytes) of the received blob or None."""
        with self._lock:
            return self._con.execute(
                "SELECT rel_path, sha256, size_bytes FROM artifact_blobs "
                "WHERE artifact_id = ?",
                [artifact_id],
            ).fetchone()

    def record_artifact_blob(
        self, artifact_id: str, rel_path: str, sha256: str, size_bytes: int
    ) -> None:
        """Idempotently record a received blob; first receipt wins."""
        received_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                self._con.execute(
                    "INSERT INTO artifact_blobs (artifact_id, rel_path, sha256, "
                    "size_bytes, received_ns) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(artifact_id) DO NOTHING",
                    [artifact_id, rel_path, sha256, size_bytes, received_ns],
                )
                self._con.execute(
                    "UPDATE artifacts SET received_ns = ? WHERE artifact_id = ? "
                    "AND received_ns IS NULL",
                    [received_ns, artifact_id],
                )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def _curation_row(self, sweep_id: str) -> tuple | None:
        return self._con.execute(
            "SELECT c.archived_ns, c.invalid_ns, c.invalid_reason "
            "FROM sweeps s LEFT JOIN sweep_curation c ON c.sweep_id = s.sweep_id "
            "WHERE s.sweep_id = ?",
            [sweep_id],
        ).fetchone()

    def archive_sweep(self, sweep_id: str) -> None:
        """Mark ``sweep_id`` archived; retrying while archived is a no-op."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._curation_row(sweep_id)
                if row is None:
                    raise SweepNotFoundError(f"no sweep with id {sweep_id}")
                if row[0] is None:
                    self._con.execute(
                        "INSERT INTO sweep_curation "
                        "(sweep_id, archived_ns, updated_ns) VALUES (?, ?, ?) "
                        "ON CONFLICT(sweep_id) DO UPDATE SET "
                        "archived_ns = excluded.archived_ns, "
                        "updated_ns = excluded.updated_ns",
                        [sweep_id, now_ns, now_ns],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def restore_sweep(self, sweep_id: str) -> None:
        """Clear archived; rejected while the sweep remains invalid."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._curation_row(sweep_id)
                if row is None:
                    raise SweepNotFoundError(f"no sweep with id {sweep_id}")
                if row[1] is not None:
                    raise SweepStillInvalidError(
                        f"sweep {sweep_id} remains invalid; "
                        "restore validity before unarchiving"
                    )
                if row[0] is not None:
                    self._con.execute(
                        "UPDATE sweep_curation SET archived_ns = NULL, "
                        "updated_ns = ? WHERE sweep_id = ?",
                        [now_ns, sweep_id],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def mark_sweep_invalid(self, sweep_id: str, reason: str) -> None:
        """Mark ``sweep_id`` scientifically invalid with a trimmed reason of
        1..500 characters; archives the sweep when no archived fact exists."""
        trimmed = reason.strip()
        if not trimmed or len(trimmed) > 500:
            raise InvalidCurationReasonError(
                f"invalid reason must be 1..500 characters after trimming, "
                f"got {len(trimmed)}"
            )
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._curation_row(sweep_id)
                if row is None:
                    raise SweepNotFoundError(f"no sweep with id {sweep_id}")
                if row[1] is None or row[2] != trimmed:
                    self._con.execute(
                        "INSERT INTO sweep_curation "
                        "(sweep_id, archived_ns, invalid_ns, invalid_reason, "
                        "updated_ns) VALUES (?, COALESCE(?, ?), ?, ?, ?) "
                        "ON CONFLICT(sweep_id) DO UPDATE SET "
                        "archived_ns = excluded.archived_ns, "
                        "invalid_ns = excluded.invalid_ns, "
                        "invalid_reason = excluded.invalid_reason, "
                        "updated_ns = excluded.updated_ns",
                        [sweep_id, row[0], now_ns, now_ns, trimmed, now_ns],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def restore_sweep_validity(self, sweep_id: str) -> None:
        """Clear the invalid facts; the archived fact is left unchanged."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._curation_row(sweep_id)
                if row is None:
                    raise SweepNotFoundError(f"no sweep with id {sweep_id}")
                if row[1] is not None:
                    self._con.execute(
                        "UPDATE sweep_curation SET invalid_ns = NULL, "
                        "invalid_reason = NULL, updated_ns = ? WHERE sweep_id = ?",
                        [now_ns, sweep_id],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def investigations(
        self, project: str, include_archived: bool = False
    ) -> list[dict]:
        """List a project's investigations by name, each with its members."""
        columns = ", ".join(_INVESTIGATION_COLUMNS)
        with self._lock:
            scope = "WHERE project = ?"
            if not include_archived:
                scope += " AND archived_ns IS NULL"
            rows = self._con.execute(
                f"SELECT {columns} FROM investigations {scope} ORDER BY name",
                [project],
            ).fetchall()
            pairs = self._con.execute(
                "SELECT investigation_id, sweep_id FROM investigation_sweeps "
                "WHERE investigation_id IN "
                "(SELECT investigation_id FROM investigations "
                "WHERE project = ?) ORDER BY added_ns, sweep_id",
                [project],
            ).fetchall()
        members: dict[str, list[str]] = {}
        for investigation_id, sweep_id in pairs:
            members.setdefault(investigation_id, []).append(sweep_id)
        return [
            self._investigation_record(row, tuple(members.get(row[0], ())))
            for row in rows
        ]

    def investigation(self, investigation_id: str) -> dict | None:
        """Return one investigation with its members, or None."""
        with self._lock:
            row = self._fetch_investigation(investigation_id)
            if row is None:
                return None
            return self._investigation_record(
                row, self._fetch_members(investigation_id)
            )

    def investigation_by_name(self, project: str, name: str) -> dict | None:
        """Return the project's investigation ``name`` with members, or None."""
        with self._lock:
            row = self._con.execute(
                f"SELECT {', '.join(_INVESTIGATION_COLUMNS)} FROM investigations "
                "WHERE project = ? AND name = ?",
                [project, name],
            ).fetchone()
            if row is None:
                return None
            return self._investigation_record(row, self._fetch_members(row[0]))

    def create_investigation(
        self,
        investigation_id: str,
        project: str,
        name: str,
        factor: str,
        outcome: str,
        replicate_factor: str | None,
        member_sweep_ids: Sequence[str] = (),
    ) -> dict:
        """Create an investigation; an existing (project, name) with a
        matching body returns the stored record unchanged."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._con.execute(
                    f"SELECT {', '.join(_INVESTIGATION_COLUMNS)} "
                    "FROM investigations WHERE project = ? AND name = ?",
                    [project, name],
                ).fetchone()
                if row is not None:
                    if (row[3], row[4], row[5]) != (
                        factor,
                        outcome,
                        replicate_factor,
                    ):
                        raise InvestigationConflictError(
                            f"investigation {project}/{name} already exists "
                            "with a different factor/outcome body"
                        )
                    self._con.execute("COMMIT")
                    return self._investigation_record(row, self._fetch_members(row[0]))
                self._con.execute(
                    "INSERT INTO investigations (investigation_id, project, "
                    "name, factor, outcome, replicate_factor, archived_ns, "
                    "created_ns, updated_ns) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    [
                        investigation_id,
                        project,
                        name,
                        factor,
                        outcome,
                        replicate_factor,
                        now_ns,
                        now_ns,
                    ],
                )
                for sweep_id in self._validated_members(project, member_sweep_ids):
                    self._con.execute(
                        "INSERT INTO investigation_sweeps (investigation_id, "
                        "sweep_id, added_ns) VALUES (?, ?, ?)",
                        [investigation_id, sweep_id, now_ns],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise
            row = self._fetch_investigation(investigation_id)
            if row is None:
                raise StoreError(
                    f"investigation {investigation_id} missing after insert"
                )
            return self._investigation_record(
                row, self._fetch_members(investigation_id)
            )

    def set_investigation_members(
        self, investigation_id: str, member_sweep_ids: Sequence[str]
    ) -> None:
        """Replace the member set; kept sweeps keep their added_ns."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._fetch_investigation(investigation_id)
                if row is None:
                    raise InvestigationNotFoundError(
                        f"no investigation with id {investigation_id}"
                    )
                desired = self._validated_members(row[1], member_sweep_ids)
                current = self._fetch_members(investigation_id)
                current_set = set(current)
                stale = [sid for sid in current if sid not in desired]
                fresh = [sid for sid in desired if sid not in current_set]
                if stale or fresh:
                    self._remove_member_rows(investigation_id, stale)
                    for sweep_id in fresh:
                        self._con.execute(
                            "INSERT INTO investigation_sweeps (investigation_id,"
                            " sweep_id, added_ns) VALUES (?, ?, ?)",
                            [investigation_id, sweep_id, now_ns],
                        )
                    self._con.execute(
                        "UPDATE investigations SET updated_ns = ? "
                        "WHERE investigation_id = ?",
                        [now_ns, investigation_id],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def add_members(
        self, investigation_id: str, member_sweep_ids: Sequence[str]
    ) -> None:
        """Add sweeps to the member set; existing members are a no-op."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._fetch_investigation(investigation_id)
                if row is None:
                    raise InvestigationNotFoundError(
                        f"no investigation with id {investigation_id}"
                    )
                desired = self._validated_members(row[1], member_sweep_ids)
                current = set(self._fetch_members(investigation_id))
                fresh = [sid for sid in desired if sid not in current]
                if fresh:
                    for sweep_id in fresh:
                        self._con.execute(
                            "INSERT INTO investigation_sweeps (investigation_id,"
                            " sweep_id, added_ns) VALUES (?, ?, ?)",
                            [investigation_id, sweep_id, now_ns],
                        )
                    self._con.execute(
                        "UPDATE investigations SET updated_ns = ? "
                        "WHERE investigation_id = ?",
                        [now_ns, investigation_id],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def remove_members(
        self, investigation_id: str, member_sweep_ids: Sequence[str]
    ) -> None:
        """Drop sweeps from the member set; non-members are a no-op."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._fetch_investigation(investigation_id)
                if row is None:
                    raise InvestigationNotFoundError(
                        f"no investigation with id {investigation_id}"
                    )
                current = set(self._fetch_members(investigation_id))
                drop = [
                    sid for sid in dict.fromkeys(member_sweep_ids) if sid in current
                ]
                if drop:
                    self._remove_member_rows(investigation_id, drop)
                    self._con.execute(
                        "UPDATE investigations SET updated_ns = ? "
                        "WHERE investigation_id = ?",
                        [now_ns, investigation_id],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def archive_investigation(self, investigation_id: str, ns: int) -> None:
        """Mark the investigation archived at ``ns``; retrying is a no-op."""
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._fetch_investigation(investigation_id)
                if row is None:
                    raise InvestigationNotFoundError(
                        f"no investigation with id {investigation_id}"
                    )
                if row[6] is None:
                    self._con.execute(
                        "UPDATE investigations SET archived_ns = ?, "
                        "updated_ns = ? WHERE investigation_id = ?",
                        [ns, ns, investigation_id],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def restore_investigation(self, investigation_id: str) -> None:
        """Clear the archived flag; already-active investigations are a no-op."""
        now_ns = time.time_ns()
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                row = self._fetch_investigation(investigation_id)
                if row is None:
                    raise InvestigationNotFoundError(
                        f"no investigation with id {investigation_id}"
                    )
                if row[6] is not None:
                    self._con.execute(
                        "UPDATE investigations SET archived_ns = NULL, "
                        "updated_ns = ? WHERE investigation_id = ?",
                        [now_ns, investigation_id],
                    )
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def _fetch_investigation(self, investigation_id: str) -> tuple | None:
        return self._con.execute(
            f"SELECT {', '.join(_INVESTIGATION_COLUMNS)} FROM investigations "
            "WHERE investigation_id = ?",
            [investigation_id],
        ).fetchone()

    def _fetch_members(self, investigation_id: str) -> tuple[str, ...]:
        return tuple(
            row[0]
            for row in self._con.execute(
                "SELECT sweep_id FROM investigation_sweeps "
                "WHERE investigation_id = ? ORDER BY added_ns, sweep_id",
                [investigation_id],
            )
        )

    def _investigation_record(self, row: tuple, members: tuple[str, ...]) -> dict:
        record = dict(zip(_INVESTIGATION_COLUMNS, row, strict=True))
        record["members"] = members
        return record

    def _validated_members(self, project: str, sweep_ids: Sequence[str]) -> list[str]:
        unique = list(dict.fromkeys(sweep_ids))
        if not unique:
            return []
        placeholders = ", ".join("?" * len(unique))
        found = {
            row[0]: row[1]
            for row in self._con.execute(
                f"SELECT sweep_id, project FROM sweeps "
                f"WHERE sweep_id IN ({placeholders})",
                unique,
            )
        }
        missing = sorted(sid for sid in unique if sid not in found)
        if missing:
            raise SweepNotFoundError(f"no sweep with id {missing[0]}")
        foreign = sorted(sid for sid in unique if found[sid] != project)
        if foreign:
            raise CrossProjectSweepError(
                f"sweep {foreign[0]} belongs to project "
                f"{found[foreign[0]]!r}, not {project!r}"
            )
        return unique

    def _remove_member_rows(self, investigation_id: str, sweep_ids: list[str]) -> None:
        placeholders = ", ".join("?" * len(sweep_ids))
        self._con.execute(
            f"DELETE FROM investigation_sweeps WHERE investigation_id = ? "
            f"AND sweep_id IN ({placeholders})",
            [investigation_id, *sweep_ids],
        )

    def backup_to(self, dest: str | Path) -> None:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            with self._lock:
                target = sqlite3.connect(tmp)
                try:
                    self._con.backup(target)
                finally:
                    target.close()
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def verify(self) -> None:
        integrity = [row[0] for row in self._con.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise StoreError(f"integrity_check failed: {integrity}")
        violations = self._con.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise StoreError(f"foreign_key_check reported violations: {violations}")


def _archive_database(db_path: Path, dest: Path) -> None:
    try:
        source = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(dest)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
    except sqlite3.Error:
        # Unopenable or corrupt: fall back to a plain byte copy, including
        # WAL sidecars so committed transactions survive.
        shutil.copy2(db_path, dest)
        for suffix in ("-wal", "-shm"):
            sidecar = db_path.parent / (db_path.name + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, dest.parent / (dest.name + suffix))


def _write_checksums(root: Path) -> None:
    lines = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(root).as_posix()}")
    (root / _ARCHIVE_MANIFEST).write_text("\n".join(lines) + "\n")


def _fresh_dest(dest_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f") + "Z"
    final = dest_dir / stamp
    suffix = 1
    while final.exists():
        final = dest_dir / f"{stamp}-{suffix}"
        suffix += 1
    return final


def archive_v2(
    db_path: str | Path,
    artifacts_root: str | Path,
    dest_dir: str | Path,
) -> Path:
    """Archive a legacy v2 database and its artifact root for the v3 cutover.

    Best-effort online backup of the SQLite file (plain copy when the file
    is not openable), recursive copy of the artifact root when present, and
    a SHA256SUMS manifest over every archived file — all written into
    ``dest_dir/<timestamp>/`` via a temp directory and an atomic rename.
    Never writes to the sources; raises :class:`StoreError` and leaves no
    partial archive behind on failure.
    """
    db_path = Path(db_path)
    artifacts_root = Path(artifacts_root)
    dest_dir = Path(dest_dir)
    tmp: Path | None = None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-archive-", dir=dest_dir))
        _archive_database(db_path, tmp / db_path.name)
        if artifacts_root.is_dir():
            shutil.copytree(artifacts_root, tmp / artifacts_root.name)
        elif artifacts_root.exists():
            shutil.copy2(artifacts_root, tmp / artifacts_root.name)
        _write_checksums(tmp)
        final = _fresh_dest(dest_dir)
        os.rename(tmp, final)
        return final
    except Exception as e:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
        raise StoreError(f"archiving {db_path} into {dest_dir} failed: {e}") from e
