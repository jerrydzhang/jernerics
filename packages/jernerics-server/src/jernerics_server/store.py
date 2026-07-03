import sqlite3
import threading
from pathlib import Path
from typing import Self

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
    UNIQUE (project, study_name, trial_id, seq)
) STRICT
"""

_CREATE_METRICS = """
CREATE TABLE IF NOT EXISTS metrics (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    value REAL NOT NULL,
    step INTEGER,
    UNIQUE (project, study_name, trial_id, seq)
) STRICT
"""

_CREATE_RESULTS = """
CREATE TABLE IF NOT EXISTS results (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    UNIQUE (project, study_name, trial_id, seq)
) STRICT
"""

_CREATE_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS artifacts (
    project TEXT NOT NULL,
    study_name TEXT NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    key TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    UNIQUE (project, study_name, trial_id, seq)
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


class Store:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        for stmt in (
            _CREATE_PARAMS,
            _CREATE_METRICS,
            _CREATE_RESULTS,
            _CREATE_ARTIFACTS,
            _CREATE_SWEEP_META,
            _CREATE_TRIAL_END,
        ):
            self._con.execute(stmt)
        self._con.commit()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._con.close()

    def insert_event(self, envelope: dict) -> None:
        with self._lock:
            if "param" in envelope:
                self._insert_param(envelope)
            elif "metric" in envelope:
                self._insert_metric(envelope)
            elif "result" in envelope:
                self._insert_result(envelope)
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

    def _insert_param(self, env: dict) -> None:
        p = env["param"]
        val = p["value"]
        self._con.execute(
            "INSERT OR IGNORE INTO params VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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

    def _insert_metric(self, env: dict) -> None:
        m = env["metric"]
        self._con.execute(
            "INSERT OR IGNORE INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env["timestamp_ns"],
                env["seq"],
                m["key"],
                m["value"],
                m.get("step"),
            ],
        )

    def _insert_result(self, env: dict) -> None:
        r = env["result"]
        self._con.execute(
            "INSERT OR IGNORE INTO results VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env["timestamp_ns"],
                env["seq"],
                r["key"],
                r["value"],
            ],
        )

    def _insert_artifact(self, env: dict) -> None:
        a = env["artifact"]
        self._con.execute(
            "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                env["project"],
                env["study_name"],
                env["trial_id"],
                env["timestamp_ns"],
                env["seq"],
                a["key"],
                a["filename"],
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
