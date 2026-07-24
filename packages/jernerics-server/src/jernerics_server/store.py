import sqlite3
import threading
from pathlib import Path
from typing import Self

_SCHEMA_VERSION = 2

# Old (v0/v1) + current table names; dropped on a fresh-start migration.
_ALL_TABLES = (
    "tracked_values",
    "params",
    "artifacts",
    "sweep_meta",
    "trial_end",
    "metrics",
    "results",
)

_CREATE_TRACKED_VALUES = """
CREATE TABLE IF NOT EXISTS tracked_values (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL DEFAULT 0,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    step INTEGER,
    context TEXT NOT NULL DEFAULT '{}',
    value_type TEXT NOT NULL CHECK (
        (value_type = 'scalar' AND text_val IS NULL)
        OR (value_type = 'json' AND text_val IS NOT NULL AND scalar_val IS NULL)
    ),
    scalar_val REAL,
    text_val TEXT,
    UNIQUE (project, study_name, trial_id, run_id, seq)
) STRICT
"""

_CREATE_IDX_VALUES_STUDY_KEY = """
CREATE INDEX IF NOT EXISTS idx_values_study_key
ON tracked_values(project, study_name, key)
"""

_CREATE_IDX_VALUES_STUDY_KEY_STEP = """
CREATE INDEX IF NOT EXISTS idx_values_study_key_step
ON tracked_values(project, study_name, key, step)
"""

_CREATE_PARAMS = """
CREATE TABLE IF NOT EXISTS params (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    float_val REAL,
    int_val INTEGER,
    string_val TEXT,
    bool_val INTEGER,
    UNIQUE (project, study_name, trial_id, key)
) STRICT
"""

_CREATE_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS artifacts (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL DEFAULT 0,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '{}',
    filename TEXT NOT NULL DEFAULT '',
    UNIQUE (project, study_name, trial_id, run_id, seq)
) STRICT
"""

_CREATE_SWEEP_META = """
CREATE TABLE IF NOT EXISTS sweep_meta (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    git_hash TEXT,
    config TEXT,
    UNIQUE (project, study_name, trial_id, seq)
) STRICT
"""

_CREATE_TRIAL_END = """
CREATE TABLE IF NOT EXISTS trial_end (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    UNIQUE (project, study_name, trial_id, seq)
) STRICT
"""

_CREATE_STATEMENTS = (
    _CREATE_TRACKED_VALUES,
    _CREATE_IDX_VALUES_STUDY_KEY,
    _CREATE_IDX_VALUES_STUDY_KEY_STEP,
    _CREATE_PARAMS,
    _CREATE_ARTIFACTS,
    _CREATE_SWEEP_META,
    _CREATE_TRIAL_END,
)


class Store:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._con.commit()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._con.close()

    def _migrate(self) -> None:
        version = self._con.execute("PRAGMA user_version").fetchone()[0]
        if version < _SCHEMA_VERSION:
            for table in _ALL_TABLES:
                self._con.execute(f"DROP TABLE IF EXISTS {table}")
            for stmt in _CREATE_STATEMENTS:
                self._con.execute(stmt)
            self._con.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        else:
            for stmt in _CREATE_STATEMENTS:
                self._con.execute(stmt)

    def insert_event(self, envelope: dict) -> None:
        with self._lock:
            if "value" in envelope:
                self._insert_value(envelope)
            elif "param" in envelope:
                self._insert_param(envelope)
            elif "artifact" in envelope:
                self._insert_artifact(envelope)
            elif "sweep_meta" in envelope:
                self._insert_sweep_meta(envelope)
            elif "trial_end" in envelope:
                self._insert_trial_end(envelope)
            self._con.commit()

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

    def _insert_value(self, env: dict) -> None:
        v = env["value"]
        value_json = v.get("value_json")
        if value_json is not None:
            value_type = "json"
            scalar_val = None
            text_val = value_json
        else:
            value_type = "scalar"
            scalar_val = v.get("value")
            text_val = None
        self._con.execute(
            "INSERT OR IGNORE INTO tracked_values VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env.get("run_id", 0),
                env["timestamp_ns"],
                env["seq"],
                v["key"],
                v.get("step"),
                v.get("context") or "{}",
                value_type,
                scalar_val,
                text_val,
            ],
        )

    def _insert_param(self, env: dict) -> None:
        p = env["param"]
        val = p["value"]
        self._con.execute(
            "INSERT OR REPLACE INTO params VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env["timestamp_ns"],
                env["seq"],
                p["key"],
                val.get("float_val"),
                val.get("int_val"),
                val.get("string_val"),
                val.get("bool_val"),
            ],
        )

    def _insert_artifact(self, env: dict) -> None:
        a = env["artifact"]
        self._con.execute(
            "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env.get("run_id", 0),
                env["timestamp_ns"],
                env["seq"],
                a["key"],
                a.get("context") or "{}",
                a.get("filename", ""),
            ],
        )

    def _insert_sweep_meta(self, env: dict) -> None:
        s = env["sweep_meta"]
        self._con.execute(
            "INSERT OR IGNORE INTO sweep_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env["timestamp_ns"],
                env["seq"],
                s.get("git_hash"),
                s.get("config"),
            ],
        )

    def _insert_trial_end(self, env: dict) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO trial_end VALUES (?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env["timestamp_ns"],
                env["seq"],
            ],
        )
