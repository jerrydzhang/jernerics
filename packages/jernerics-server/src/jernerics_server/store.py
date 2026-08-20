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
from collections.abc import Callable
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

SCHEMA_VERSION = 3

_V2_TABLES = ("sweep_meta", "trial_end", "params")

_ARCHIVE_MANIFEST = "SHA256SUMS"


class StoreError(Exception):
    """Base class for store failures."""


class LegacyStoreError(StoreError):
    """A v1/v2 tracking database is present; automatic startup is refused."""


class FutureSchemaError(StoreError):
    """The database schema version is newer than this build supports."""


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


_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    3: _migrate_to_v3,
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
            cursor = con.execute(sql, params or [])
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return columns, rows
        finally:
            con.close()

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
