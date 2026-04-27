from jernerics_proto import (
    ArtifactEvent,
    Envelope,
    MetricEvent,
    ParamEvent,
    ResultEvent,
    SweepMetaEvent,
    TrialEndEvent,
    Value,
)
from jernerics_server.store import DuckDBStore


def _ts(n: int = 0) -> int:
    return 1_000_000_000 + n


_seq_counter = 0


def _next_seq() -> int:
    global _seq_counter
    _seq_counter += 1
    return _seq_counter - 1


def _reset_seq() -> None:
    global _seq_counter
    _seq_counter = 0


def _envelope(
    payload_name: str,
    payload,
    project: str = "p",
    study: str = "s",
    trial: int = 0,
    ts: int = 0,
) -> Envelope:
    return Envelope(
        project=project,
        study_name=study,
        trial_id=trial,
        timestamp_ns=_ts(ts),
        seq=_next_seq(),
        **{payload_name: payload},
    )


class TestInsertParam:
    def setup_method(self):
        _reset_seq()

    def test_float(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.001)))
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, key, float_val, seq FROM params"
            ).fetchall()
            assert rows == [("p", "lr", 0.001, 0)]

    def test_int(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("param", ParamEvent(key="batch", value=Value(int_val=32)))
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, key, int_val FROM params"
            ).fetchall()
            assert rows == [("p", "batch", 32)]

    def test_string(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope(
                "param", ParamEvent(key="name", value=Value(string_val="adam"))
            )
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, key, string_val FROM params"
            ).fetchall()
            assert rows == [("p", "name", "adam")]

    def test_bool(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope(
                "param", ParamEvent(key="augment", value=Value(bool_val=True))
            )
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, key, bool_val FROM params"
            ).fetchall()
            assert rows == [("p", "augment", True)]

    def test_non_active_columns_are_null(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.01)))
            store.insert_event(env)

            row = store._con.execute(
                "SELECT int_val, string_val, bool_val FROM params"
            ).fetchone()
            assert row is not None
            int_val, string_val, bool_val = row
            assert int_val is None
            assert string_val is None
            assert bool_val is None


class TestInsertMetric:
    def setup_method(self):
        _reset_seq()

    def test_with_step(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("metric", MetricEvent(key="loss", value=0.5, step=100))
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, key, value, step FROM metrics"
            ).fetchall()
            assert rows == [("p", "loss", 0.5, 100)]

    def test_without_step_stores_null(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("metric", MetricEvent(key="loss", value=0.5, step=-1))
            store.insert_event(env)

            row = store._con.execute("SELECT step FROM metrics").fetchone()
            assert row is not None
            step = row[0]
            assert step is None


class TestInsertResult:
    def setup_method(self):
        _reset_seq()

    def test_json_string(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("result", ResultEvent(key="confusion", value='{"tp": 90}'))
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, key, value FROM results"
            ).fetchall()
            assert rows == [("p", "confusion", '{"tp": 90}')]


class TestInsertArtifact:
    def setup_method(self):
        _reset_seq()

    def test_local_path(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope(
                "artifact", ArtifactEvent(key="model", local_path="/tmp/model.pt")
            )
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, key, local_path FROM artifacts"
            ).fetchall()
            assert rows == [("p", "model", "/tmp/model.pt")]


class TestInsertSweepMeta:
    def setup_method(self):
        _reset_seq()

    def test_git_hash_and_config(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope(
                "sweep_meta",
                SweepMetaEvent(git_hash="abc123", config="base = {}"),
            )
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, git_hash, config FROM sweep_meta"
            ).fetchall()
            assert rows == [("p", "abc123", "base = {}")]


class TestInsertTrialEnd:
    def setup_method(self):
        _reset_seq()

    def test_inserts_marker(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("trial_end", TrialEndEvent())
            store.insert_event(env)

            rows = store._con.execute(
                "SELECT project, study_name, trial_id, seq FROM trial_end"
            ).fetchall()
            assert rows == [("p", "s", 0, 0)]


class TestIdempotency:
    def setup_method(self):
        _reset_seq()

    def test_duplicate_seq_ignored(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.01)))
            store.insert_event(env)
            store.insert_event(env)

            row = store._con.execute("SELECT COUNT(*) FROM params").fetchone()
            assert row is not None
            assert row[0] == 1

    def test_different_seq_accepted(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            env0 = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.01)))
            env1 = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.1)))
            store.insert_event(env0)
            store.insert_event(env1)

            rows = store._con.execute(
                "SELECT seq, float_val FROM params ORDER BY seq"
            ).fetchall()
            assert rows == [(0, 0.01), (1, 0.1)]


class TestInsertMultiple:
    def setup_method(self):
        _reset_seq()

    def test_common_fields_correct(self, tmp_path):
        with DuckDBStore(tmp_path / "test.duckdb") as store:
            store.insert_event(
                _envelope(
                    "param",
                    ParamEvent(key="lr", value=Value(float_val=0.01)),
                    project="p",
                    study="exp1",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _envelope(
                    "metric",
                    MetricEvent(key="loss", value=0.5, step=10),
                    project="p",
                    study="exp1",
                    trial=0,
                    ts=2,
                )
            )
            store.insert_event(
                _envelope(
                    "param",
                    ParamEvent(key="lr", value=Value(float_val=0.1)),
                    project="p",
                    study="exp1",
                    trial=1,
                    ts=3,
                )
            )

            param_row = store._con.execute("SELECT COUNT(*) FROM params").fetchone()
            assert param_row is not None
            param_count = param_row[0]
            metric_row = store._con.execute("SELECT COUNT(*) FROM metrics").fetchone()
            assert metric_row is not None
            metric_count = metric_row[0]
            assert param_count == 2
            assert metric_count == 1

            lr_values = store._con.execute(
                "SELECT trial_id, float_val FROM params"
                " WHERE key = 'lr' ORDER BY trial_id"
            ).fetchall()
            assert lr_values == [(0, 0.01), (1, 0.1)]
