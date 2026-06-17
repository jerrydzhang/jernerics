import sqlite3
import threading
from pathlib import Path
from typing import Self, TypedDict

from jernerics_proto import Envelope


class StudySummary(TypedDict):
    trial_count: int
    completed_count: int
    param_keys: list[str]
    final_metric_keys: list[str]
    artifact_keys: list[str]


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

    def insert_event(self, envelope: Envelope) -> None:
        payload = envelope.WhichOneof("payload")
        with self._lock:
            if payload == "param":
                self._insert_param(envelope)
            elif payload == "metric":
                self._insert_metric(envelope)
            elif payload == "result":
                self._insert_result(envelope)
            elif payload == "artifact":
                self._insert_artifact(envelope)
            elif payload == "sweep_meta":
                self._insert_sweep_meta(envelope)
            elif payload == "trial_end":
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

    def list_sweeps(self) -> list[dict]:
        sql = """
        SELECT
            project,
            study_name,
            (
                SELECT COUNT(DISTINCT trial_id)
                FROM (
                    SELECT trial_id FROM params
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT trial_id FROM metrics
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT trial_id FROM results
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT trial_id FROM artifacts
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT trial_id FROM sweep_meta
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT trial_id FROM trial_end
                    WHERE project = s.project AND study_name = s.study_name
                )
            ) AS trial_count,
            (
                SELECT COUNT(DISTINCT trial_id)
                FROM trial_end
                WHERE project = s.project AND study_name = s.study_name
            ) AS completed_count,
            (
                SELECT MAX(timestamp_ns)
                FROM (
                    SELECT timestamp_ns FROM params
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT timestamp_ns FROM metrics
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT timestamp_ns FROM results
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT timestamp_ns FROM artifacts
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT timestamp_ns FROM sweep_meta
                    WHERE project = s.project AND study_name = s.study_name
                    UNION SELECT timestamp_ns FROM trial_end
                    WHERE project = s.project AND study_name = s.study_name
                )
            ) AS last_event_timestamp_ns
        FROM (
            SELECT DISTINCT project, study_name FROM params
            UNION
            SELECT DISTINCT project, study_name FROM metrics
            UNION
            SELECT DISTINCT project, study_name FROM results
            UNION
            SELECT DISTINCT project, study_name FROM artifacts
            UNION
            SELECT DISTINCT project, study_name FROM sweep_meta
            UNION
            SELECT DISTINCT project, study_name FROM trial_end
        ) s
        ORDER BY project, study_name
        """
        con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            cursor = con.execute(sql)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            return [dict(zip(columns, row, strict=True)) for row in rows]
        finally:
            con.close()

    def list_trials(self, project: str, study_name: str) -> list[dict]:
        sql = """
        SELECT
            t.trial_id,
            CASE WHEN te.trial_id IS NOT NULL THEN 'complete' ELSE 'incomplete' END
            AS status
        FROM (
            SELECT DISTINCT trial_id FROM params
            WHERE project = ? AND study_name = ?
            UNION
            SELECT DISTINCT trial_id FROM metrics
            WHERE project = ? AND study_name = ?
            UNION
            SELECT DISTINCT trial_id FROM results
            WHERE project = ? AND study_name = ?
            UNION
            SELECT DISTINCT trial_id FROM artifacts
            WHERE project = ? AND study_name = ?
            UNION
            SELECT DISTINCT trial_id FROM sweep_meta
            WHERE project = ? AND study_name = ?
            UNION
            SELECT DISTINCT trial_id FROM trial_end
            WHERE project = ? AND study_name = ?
        ) t
        LEFT JOIN trial_end te ON t.trial_id = te.trial_id
            AND te.project = ? AND te.study_name = ?
        ORDER BY t.trial_id
        """
        params = [project, study_name] * 7
        con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            cursor = con.execute(sql, params)
            columns = [desc[0] for desc in cursor.description]
            rows = cursor.fetchall()
            trials = []
            for row in rows:
                trial = dict(zip(columns, row, strict=True))
                trial_id = trial["trial_id"]

                # Fetch params
                cursor2 = con.execute(
                    "SELECT key, float_val, int_val, string_val, bool_val "
                    "FROM params WHERE project = ? AND study_name = ? AND trial_id = ?",
                    [project, study_name, trial_id],
                )
                params_dict = {}
                for key, float_val, int_val, string_val, bool_val in cursor2.fetchall():
                    if float_val is not None:
                        params_dict[key] = float_val
                    elif int_val is not None:
                        params_dict[key] = int_val
                    elif string_val is not None:
                        params_dict[key] = string_val
                    elif bool_val is not None:
                        params_dict[key] = bool(bool_val)
                trial["params"] = params_dict

                # Fetch final metrics (step IS NULL)
                cursor3 = con.execute(
                    "SELECT key, value FROM metrics "
                    "WHERE project = ? AND study_name = ? "
                    "AND trial_id = ? AND step IS NULL",
                    [project, study_name, trial_id],
                )
                trial["final_metrics"] = {
                    key: value for key, value in cursor3.fetchall()
                }

                # Fetch artifact keys
                cursor4 = con.execute(
                    "SELECT key FROM artifacts "
                    "WHERE project = ? AND study_name = ? AND trial_id = ?",
                    [project, study_name, trial_id],
                )
                trial["artifact_keys"] = [row[0] for row in cursor4.fetchall()]

                trials.append(trial)
            return trials
        finally:
            con.close()

    def _insert_param(self, env: Envelope) -> None:
        p = env.param
        val = p.value
        tag = val.WhichOneof("value")
        float_val = val.float_val if tag == "float_val" else None
        int_val = val.int_val if tag == "int_val" else None
        string_val = val.string_val if tag == "string_val" else None
        bool_val = val.bool_val if tag == "bool_val" else None
        self._con.execute(
            "INSERT OR IGNORE INTO params VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                env.project,
                env.study_name,
                env.trial_id,
                env.timestamp_ns,
                env.seq,
                p.key,
                float_val,
                int_val,
                string_val,
                bool_val,
            ],
        )

    def _insert_metric(self, env: Envelope) -> None:
        m = env.metric
        step = m.step if m.step != -1 else None
        self._con.execute(
            "INSERT OR IGNORE INTO metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                env.project,
                env.study_name,
                env.trial_id,
                env.timestamp_ns,
                env.seq,
                m.key,
                m.value,
                step,
            ],
        )

    def _insert_result(self, env: Envelope) -> None:
        r = env.result
        self._con.execute(
            "INSERT OR IGNORE INTO results VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                env.project,
                env.study_name,
                env.trial_id,
                env.timestamp_ns,
                env.seq,
                r.key,
                r.value,
            ],
        )

    def _insert_artifact(self, env: Envelope) -> None:
        a = env.artifact
        self._con.execute(
            "INSERT OR IGNORE INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                env.project,
                env.study_name,
                env.trial_id,
                env.timestamp_ns,
                env.seq,
                a.key,
                a.filename,
            ],
        )

    def _insert_sweep_meta(self, env: Envelope) -> None:
        s = env.sweep_meta
        self._con.execute(
            "INSERT OR IGNORE INTO sweep_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                env.project,
                env.study_name,
                env.trial_id,
                env.timestamp_ns,
                env.seq,
                s.git_hash,
                s.config,
            ],
        )

    def _insert_trial_end(self, env: Envelope) -> None:
        self._con.execute(
            "INSERT OR IGNORE INTO trial_end VALUES (?, ?, ?, ?, ?)",
            [env.project, env.study_name, env.trial_id, env.timestamp_ns, env.seq],
        )

    def get_study_summary(self, project: str, study_name: str) -> StudySummary | None:
        sql = """
        SELECT
            (
                SELECT COUNT(DISTINCT trial_id)
                FROM (
                    SELECT trial_id FROM params
                    WHERE project = ? AND study_name = ?
                    UNION SELECT trial_id FROM metrics
                    WHERE project = ? AND study_name = ?
                    UNION SELECT trial_id FROM results
                    WHERE project = ? AND study_name = ?
                    UNION SELECT trial_id FROM artifacts
                    WHERE project = ? AND study_name = ?
                    UNION SELECT trial_id FROM sweep_meta
                    WHERE project = ? AND study_name = ?
                    UNION SELECT trial_id FROM trial_end
                    WHERE project = ? AND study_name = ?
                )
            ) AS trial_count,
            (
                SELECT COUNT(DISTINCT trial_id)
                FROM trial_end
                WHERE project = ? AND study_name = ?
            ) AS completed_count
        """
        params = [project, study_name] * 7
        con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            cursor = con.execute(sql, params)
            row = cursor.fetchone()
            if row is None:
                return None
            trial_count, completed_count = row
            if trial_count == 0:
                return None
        finally:
            con.close()

        param_keys = self._get_keys(project, study_name, "params")
        final_metric_keys = self._get_final_metric_keys(project, study_name)
        artifact_keys = self._get_keys(project, study_name, "artifacts")

        return {
            "trial_count": trial_count,
            "completed_count": completed_count,
            "param_keys": param_keys,
            "final_metric_keys": final_metric_keys,
            "artifact_keys": artifact_keys,
        }

    def get_shared_final_metrics(
        self, project: str, left_study: str, right_study: str
    ) -> dict[str, dict[str, list[float]]]:
        left_summary = self.get_study_summary(project, left_study)
        right_summary = self.get_study_summary(project, right_study)
        if left_summary is None or right_summary is None:
            return {}

        left_metric_keys: list[str] = left_summary["final_metric_keys"]
        right_metric_keys: list[str] = right_summary["final_metric_keys"]
        shared_keys = set(left_metric_keys) & set(right_metric_keys)
        if not shared_keys:
            return {}

        result: dict[str, dict[str, list[float]]] = {}
        for key in shared_keys:
            left_values = self._get_final_metric_values(project, left_study, str(key))
            right_values = self._get_final_metric_values(project, right_study, str(key))
            result[str(key)] = {"left": left_values, "right": right_values}

        return result

    def _get_keys(self, project: str, study_name: str, table: str) -> list[str]:
        sql = f"SELECT DISTINCT key FROM {table} WHERE project = ? AND study_name = ?"
        con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            cursor = con.execute(sql, [project, study_name])
            return [row[0] for row in cursor.fetchall()]
        finally:
            con.close()

    def _get_final_metric_keys(self, project: str, study_name: str) -> list[str]:
        sql = (
            "SELECT DISTINCT key FROM metrics "
            "WHERE project = ? AND study_name = ? AND step IS NULL"
        )
        con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            cursor = con.execute(sql, [project, study_name])
            return [row[0] for row in cursor.fetchall()]
        finally:
            con.close()

    def _get_final_metric_values(
        self, project: str, study_name: str, key: str
    ) -> list[float]:
        sql = (
            "SELECT m.value "
            "FROM metrics m "
            "JOIN trial_end te ON m.trial_id = te.trial_id "
            "AND te.project = m.project AND te.study_name = m.study_name "
            "WHERE m.project = ? AND m.study_name = ? AND m.key = ? AND m.step IS NULL"
        )
        con = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True)
        try:
            cursor = con.execute(sql, [project, study_name, key])
            return [row[0] for row in cursor.fetchall()]
        finally:
            con.close()
