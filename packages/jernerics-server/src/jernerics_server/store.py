from pathlib import Path
from typing import Self

import duckdb
from jernerics_proto import Envelope

_CREATE_PARAMS = """
CREATE TABLE IF NOT EXISTS params (
    project VARCHAR NOT NULL,
    study_name VARCHAR NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns BIGINT NOT NULL,
    seq INTEGER NOT NULL,
    key VARCHAR NOT NULL,
    float_val DOUBLE,
    int_val BIGINT,
    string_val VARCHAR,
    bool_val BOOLEAN,
    UNIQUE (project, study_name, trial_id, seq)
)
"""

_CREATE_METRICS = """
CREATE TABLE IF NOT EXISTS metrics (
    project VARCHAR NOT NULL,
    study_name VARCHAR NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns BIGINT NOT NULL,
    seq INTEGER NOT NULL,
    key VARCHAR NOT NULL,
    value DOUBLE NOT NULL,
    step BIGINT,
    UNIQUE (project, study_name, trial_id, seq)
)
"""

_CREATE_RESULTS = """
CREATE TABLE IF NOT EXISTS results (
    project VARCHAR NOT NULL,
    study_name VARCHAR NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns BIGINT NOT NULL,
    seq INTEGER NOT NULL,
    key VARCHAR NOT NULL,
    value VARCHAR NOT NULL,
    UNIQUE (project, study_name, trial_id, seq)
)
"""

_CREATE_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS artifacts (
    project VARCHAR NOT NULL,
    study_name VARCHAR NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns BIGINT NOT NULL,
    seq INTEGER NOT NULL,
    key VARCHAR NOT NULL,
    filename VARCHAR NOT NULL DEFAULT '',
    UNIQUE (project, study_name, trial_id, seq)
)
"""

_CREATE_SWEEP_META = """
CREATE TABLE IF NOT EXISTS sweep_meta (
    project VARCHAR NOT NULL,
    study_name VARCHAR NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns BIGINT NOT NULL,
    seq INTEGER NOT NULL,
    git_hash VARCHAR,
    config VARCHAR,
    UNIQUE (project, study_name, trial_id, seq)
)
"""

_CREATE_TRIAL_END = """
CREATE TABLE IF NOT EXISTS trial_end (
    project VARCHAR NOT NULL,
    study_name VARCHAR NOT NULL,
    trial_id INTEGER NOT NULL,
    timestamp_ns BIGINT NOT NULL,
    seq INTEGER NOT NULL,
    UNIQUE (project, study_name, trial_id, seq)
)
"""


class DuckDBStore:
    def __init__(self, path: str | Path) -> None:
        self._con = duckdb.connect(str(path))
        for stmt in (
            _CREATE_PARAMS,
            _CREATE_METRICS,
            _CREATE_RESULTS,
            _CREATE_ARTIFACTS,
            _CREATE_SWEEP_META,
            _CREATE_TRIAL_END,
        ):
            self._con.execute(stmt)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._con.close()

    def insert_event(self, envelope: Envelope) -> None:
        payload = envelope.WhichOneof("payload")
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
