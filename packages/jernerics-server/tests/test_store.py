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
from jernerics_server.store import Store


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
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.001)))
            store.insert_event(env)

            _, rows = store.query("SELECT project, key, float_val, seq FROM params")
            assert rows == [("p", "lr", 0.001, 0)]

    def test_int(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("param", ParamEvent(key="batch", value=Value(int_val=32)))
            store.insert_event(env)

            _, rows = store.query("SELECT project, key, int_val FROM params")
            assert rows == [("p", "batch", 32)]

    def test_string(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope(
                "param", ParamEvent(key="name", value=Value(string_val="adam"))
            )
            store.insert_event(env)

            _, rows = store.query("SELECT project, key, string_val FROM params")
            assert rows == [("p", "name", "adam")]

    def test_bool(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope(
                "param", ParamEvent(key="augment", value=Value(bool_val=True))
            )
            store.insert_event(env)

            _, rows = store.query("SELECT project, key, bool_val FROM params")
            assert rows == [("p", "augment", 1)]

    def test_non_active_columns_are_null(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.01)))
            store.insert_event(env)

            _, rows = store.query("SELECT int_val, string_val, bool_val FROM params")
            assert len(rows) == 1
            int_val, string_val, bool_val = rows[0]
            assert int_val is None
            assert string_val is None
            assert bool_val is None


class TestInsertMetric:
    def setup_method(self):
        _reset_seq()

    def test_with_step(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("metric", MetricEvent(key="loss", value=0.5, step=100))
            store.insert_event(env)

            _, rows = store.query("SELECT project, key, value, step FROM metrics")
            assert rows == [("p", "loss", 0.5, 100)]

    def test_without_step_stores_null(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("metric", MetricEvent(key="loss", value=0.5, step=-1))
            store.insert_event(env)

            _, rows = store.query("SELECT step FROM metrics")
            assert len(rows) == 1
            assert rows[0][0] is None


class TestInsertResult:
    def setup_method(self):
        _reset_seq()

    def test_json_string(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("result", ResultEvent(key="confusion", value='{"tp": 90}'))
            store.insert_event(env)

            _, rows = store.query("SELECT project, key, value FROM results")
            assert rows == [("p", "confusion", '{"tp": 90}')]


class TestInsertArtifact:
    def setup_method(self):
        _reset_seq()

    def test_local_path(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("artifact", ArtifactEvent(key="model"))
            store.insert_event(env)

            _, rows = store.query("SELECT project, key FROM artifacts")
            assert rows == [("p", "model")]

    def test_filename_stored(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("artifact", ArtifactEvent(key="model", filename="model.pt"))
            store.insert_event(env)

            _, rows = store.query("SELECT key, filename FROM artifacts")
            assert rows == [("model", "model.pt")]

    def test_filename_defaults_to_empty_string(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("artifact", ArtifactEvent(key="model"))
            store.insert_event(env)

            _, rows = store.query("SELECT filename FROM artifacts")
            assert len(rows) == 1
            assert rows[0][0] == ""


class TestInsertSweepMeta:
    def setup_method(self):
        _reset_seq()

    def test_git_hash_and_config(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope(
                "sweep_meta",
                SweepMetaEvent(git_hash="abc123", config="base = {}"),
            )
            store.insert_event(env)

            _, rows = store.query("SELECT project, git_hash, config FROM sweep_meta")
            assert rows == [("p", "abc123", "base = {}")]


class TestInsertTrialEnd:
    def setup_method(self):
        _reset_seq()

    def test_inserts_marker(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("trial_end", TrialEndEvent())
            store.insert_event(env)

            _, rows = store.query(
                "SELECT project, study_name, trial_id, seq FROM trial_end"
            )
            assert rows == [("p", "s", 0, 0)]


class TestIdempotency:
    def setup_method(self):
        _reset_seq()

    def test_duplicate_seq_ignored(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.01)))
            store.insert_event(env)
            store.insert_event(env)

            _, rows = store.query("SELECT COUNT(*) FROM params")
            assert rows[0][0] == 1

    def test_different_seq_accepted(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env0 = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.01)))
            env1 = _envelope("param", ParamEvent(key="lr", value=Value(float_val=0.1)))
            store.insert_event(env0)
            store.insert_event(env1)

            _, rows = store.query("SELECT seq, float_val FROM params ORDER BY seq")
            assert rows == [(0, 0.01), (1, 0.1)]


class TestInsertMultiple:
    def setup_method(self):
        _reset_seq()

    def test_common_fields_correct(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
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

            _, rows = store.query("SELECT COUNT(*) FROM params")
            assert rows[0][0] == 2
            _, rows = store.query("SELECT COUNT(*) FROM metrics")
            assert rows[0][0] == 1

            _, rows = store.query(
                "SELECT trial_id, float_val FROM params"
                " WHERE key = 'lr' ORDER BY trial_id"
            )
            assert rows == [(0, 0.01), (1, 0.1)]


class TestListResults:
    def setup_method(self):
        _reset_seq()

    def test_no_filters_returns_all(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="accuracy", value="0.95"),
                    project="p",
                    study="s",
                    trial=1,
                    ts=2,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 85}'),
                    project="p",
                    study="other",
                    trial=0,
                    ts=3,
                )
            )

            results = store.list_results()
            assert len(results) == 3

    def test_filter_by_project(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="accuracy", value="0.95"),
                    project="p2",
                    study="s",
                    trial=0,
                    ts=2,
                )
            )

            results = store.list_results(project="p")
            assert len(results) == 1
            assert results[0]["trial_id"] == 0

    def test_filter_by_study_name(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="accuracy", value="0.95"),
                    project="p",
                    study="other",
                    trial=0,
                    ts=2,
                )
            )

            results = store.list_results(study_name="s")
            assert len(results) == 1
            assert results[0]["trial_id"] == 0

    def test_filter_by_trial_id(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="accuracy", value="0.95"),
                    project="p",
                    study="s",
                    trial=1,
                    ts=2,
                )
            )

            results = store.list_results(project="p", study_name="s", trial_id=1)
            assert len(results) == 1
            assert results[0]["trial_id"] == 1

    def test_filter_by_key(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="accuracy", value="0.95"),
                    project="p",
                    study="s",
                    trial=0,
                    ts=2,
                )
            )

            results = store.list_results(project="p", study_name="s", key="confusion")
            assert len(results) == 1
            assert results[0]["key"] == "confusion"

    def test_multiple_filters(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="accuracy", value="0.95"),
                    project="p",
                    study="s",
                    trial=0,
                    ts=2,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 85}'),
                    project="p",
                    study="s",
                    trial=1,
                    ts=3,
                )
            )

            results = store.list_results(
                project="p", study_name="s", trial_id=0, key="confusion"
            )
            assert len(results) == 1
            assert results[0]["trial_id"] == 0
            assert results[0]["key"] == "confusion"
            assert results[0]["value"] == '{"tp": 90}'

    def test_sorted_by_trial_id_then_key(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=1,
                    ts=3,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="accuracy", value="0.95"),
                    project="p",
                    study="s",
                    trial=1,
                    ts=2,
                )
            )
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 85}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )

            results = store.list_results(project="p", study_name="s")
            assert len(results) == 3
            assert results[0]["trial_id"] == 0
            assert results[0]["key"] == "confusion"
            assert results[1]["trial_id"] == 1
            assert results[1]["key"] == "accuracy"
            assert results[2]["trial_id"] == 1
            assert results[2]["key"] == "confusion"

    def test_returns_correct_fields(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _envelope(
                    "result",
                    ResultEvent(key="confusion", value='{"tp": 90}'),
                    project="p",
                    study="s",
                    trial=0,
                    ts=1,
                )
            )

            results = store.list_results(project="p", study_name="s")
            assert len(results) == 1
            assert results[0]["trial_id"] == 0
            assert results[0]["key"] == "confusion"
            assert results[0]["value"] == '{"tp": 90}'
            assert results[0]["timestamp_ns"] == _ts(1)

    def test_empty_store(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            results = store.list_results()
            assert results == []
