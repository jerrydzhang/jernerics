from jernerics_server.store import Store


def _ts(n: int = 0) -> int:
    return 1_000_000_000 + n


def _env(
    payload_key: str,
    payload: dict,
    *,
    project: str = "p",
    study: str = "s",
    trial: int = 0,
    ts: int = 0,
    seq: int = 0,
) -> dict:
    """Build a JSONL/dict envelope carrying exactly one payload key."""
    return {
        "project": project,
        "study_name": study,
        "trial_id": trial,
        "timestamp_ns": _ts(ts),
        "seq": seq,
        payload_key: payload,
    }


class TestInsertParam:
    def test_float(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("param", {"key": "lr", "value": {"float_val": 0.001}})
            )

            _, rows = store.query("SELECT project, key, float_val, seq FROM params")
            assert rows == [("p", "lr", 0.001, 0)]

    def test_int(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("param", {"key": "batch", "value": {"int_val": 32}})
            )

            _, rows = store.query("SELECT project, key, int_val FROM params")
            assert rows == [("p", "batch", 32)]

    def test_string(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("param", {"key": "name", "value": {"string_val": "adam"}})
            )

            _, rows = store.query("SELECT project, key, string_val FROM params")
            assert rows == [("p", "name", "adam")]

    def test_bool(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("param", {"key": "augment", "value": {"bool_val": True}})
            )

            _, rows = store.query("SELECT project, key, bool_val FROM params")
            assert rows == [("p", "augment", 1)]

    def test_non_active_columns_are_null(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("param", {"key": "lr", "value": {"float_val": 0.01}})
            )

            _, rows = store.query("SELECT int_val, string_val, bool_val FROM params")
            assert len(rows) == 1
            int_val, string_val, bool_val = rows[0]
            assert int_val is None
            assert string_val is None
            assert bool_val is None


class TestInsertMetric:
    def test_with_step(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("metric", {"key": "loss", "value": 0.5, "step": 100})
            )

            _, rows = store.query("SELECT project, key, value, step FROM metrics")
            assert rows == [("p", "loss", 0.5, 100)]

    def test_without_step_stores_null(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(_env("metric", {"key": "loss", "value": 0.5}))

            _, rows = store.query("SELECT step FROM metrics")
            assert len(rows) == 1
            assert rows[0][0] is None


class TestInsertResult:
    def test_json_string(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("result", {"key": "confusion", "value": '{"tp": 90}'})
            )

            _, rows = store.query("SELECT project, key, value FROM results")
            assert rows == [("p", "confusion", '{"tp": 90}')]


class TestInsertArtifact:
    def test_local_path(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(_env("artifact", {"key": "model", "filename": ""}))

            _, rows = store.query("SELECT project, key FROM artifacts")
            assert rows == [("p", "model")]

    def test_filename_stored(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("artifact", {"key": "model", "filename": "model.pt"})
            )

            _, rows = store.query("SELECT key, filename FROM artifacts")
            assert rows == [("model", "model.pt")]

    def test_filename_defaults_to_empty_string(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(_env("artifact", {"key": "model", "filename": ""}))

            _, rows = store.query("SELECT filename FROM artifacts")
            assert len(rows) == 1
            assert rows[0][0] == ""


class TestInsertSweepMeta:
    def test_git_hash_and_config(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("sweep_meta", {"git_hash": "abc123", "config": "base = {}"})
            )

            _, rows = store.query("SELECT project, git_hash, config FROM sweep_meta")
            assert rows == [("p", "abc123", "base = {}")]


class TestInsertTrialEnd:
    def test_inserts_marker(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(_env("trial_end", {}))

            _, rows = store.query(
                "SELECT project, study_name, trial_id, seq FROM trial_end"
            )
            assert rows == [("p", "s", 0, 0)]


class TestIdempotency:
    def test_duplicate_seq_ignored(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _env("param", {"key": "lr", "value": {"float_val": 0.01}})
            store.insert_event(env)
            store.insert_event(env)

            _, rows = store.query("SELECT COUNT(*) FROM params")
            assert rows[0][0] == 1

    def test_different_seq_accepted(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env0 = _env("param", {"key": "lr", "value": {"float_val": 0.01}}, seq=0)
            env1 = _env("param", {"key": "lr", "value": {"float_val": 0.1}}, seq=1)
            store.insert_event(env0)
            store.insert_event(env1)

            _, rows = store.query("SELECT seq, float_val FROM params ORDER BY seq")
            assert rows == [(0, 0.01), (1, 0.1)]


class TestInsertMultiple:
    def test_common_fields_correct(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env(
                    "param",
                    {"key": "lr", "value": {"float_val": 0.01}},
                    project="p",
                    study="exp1",
                    trial=0,
                    ts=1,
                )
            )
            store.insert_event(
                _env(
                    "metric",
                    {"key": "loss", "value": 0.5, "step": 10},
                    project="p",
                    study="exp1",
                    trial=0,
                    ts=2,
                )
            )
            store.insert_event(
                _env(
                    "param",
                    {"key": "lr", "value": {"float_val": 0.1}},
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
