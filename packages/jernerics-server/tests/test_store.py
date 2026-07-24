import sqlite3

import pytest
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
    run_id: int = 0,
    ts: int = 0,
    seq: int = 0,
) -> dict:
    """Build a JSONL/dict envelope carrying exactly one payload key."""
    return {
        "project": project,
        "study_name": study,
        "trial_id": trial,
        "run_id": run_id,
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


class TestInsertValueScalar:
    def test_with_step(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env(
                    "value",
                    {"key": "loss", "value": 0.5, "step": 100, "context": "{}"},
                )
            )

            _, rows = store.query(
                "SELECT key, value_type, scalar_val, text_val, step FROM tracked_values"
            )
            assert rows == [("loss", "scalar", 0.5, None, 100)]

    def test_without_step_stores_null(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("value", {"key": "loss", "value": 0.5, "step": None})
            )

            _, rows = store.query("SELECT step FROM tracked_values")
            assert len(rows) == 1
            assert rows[0][0] is None

    def test_context_stored(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env(
                    "value",
                    {"key": "loss", "value": 0.5, "context": '{"seed":3}'},
                )
            )

            _, rows = store.query("SELECT context FROM tracked_values")
            assert rows == [('{"seed":3}',)]

    def test_nan_value_stored_as_null(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("value", {"key": "loss", "value": None, "step": 10})
            )

            _, rows = store.query(
                "SELECT value_type, scalar_val, text_val FROM tracked_values"
            )
            assert rows == [("scalar", None, None)]


class TestInsertValueJson:
    def test_json_string(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env(
                    "value",
                    {"key": "confusion", "value_json": '{"tp": 90}', "step": None},
                )
            )

            _, rows = store.query(
                "SELECT key, value_type, scalar_val, text_val FROM tracked_values"
            )
            assert rows == [("confusion", "json", None, '{"tp": 90}')]


class TestCheckConstraint:
    def test_scalar_and_json_are_mutually_exclusive(self, tmp_path):
        with (
            Store(tmp_path / "test.sqlite") as store,
            pytest.raises(sqlite3.IntegrityError),
        ):
            store._con.execute(
                "INSERT INTO tracked_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ["p", "s", 0, 0, 1, 0, "k", None, "{}", "scalar", 0.5, "x"],
            )


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

    def test_context_stored(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env(
                    "artifact",
                    {"key": "model", "filename": "m.pt", "context": '{"seed":3}'},
                )
            )

            _, rows = store.query("SELECT context FROM artifacts")
            assert rows == [('{"seed":3}',)]


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


class TestRunIdUniqueness:
    def test_same_seq_different_run_id_both_accepted(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env(
                    "value",
                    {"key": "loss", "value": 0.1, "step": None},
                    run_id=1,
                    seq=0,
                )
            )
            store.insert_event(
                _env(
                    "value",
                    {"key": "loss", "value": 0.2, "step": None},
                    run_id=2,
                    seq=0,
                )
            )

            _, rows = store.query(
                "SELECT run_id, scalar_val FROM tracked_values ORDER BY run_id"
            )
            assert rows == [(1, 0.1), (2, 0.2)]


class TestParamUpsert:
    def test_same_key_replaces(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("param", {"key": "lr", "value": {"float_val": 0.01}}, seq=0)
            )
            store.insert_event(
                _env("param", {"key": "lr", "value": {"float_val": 0.1}}, seq=1)
            )

            _, rows = store.query("SELECT COUNT(*) FROM params")
            assert rows[0][0] == 1
            _, rows = store.query("SELECT float_val FROM params")
            assert rows == [(0.1,)]

    def test_different_key_accepted(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            store.insert_event(
                _env("param", {"key": "lr", "value": {"float_val": 0.01}}, seq=0)
            )
            store.insert_event(
                _env("param", {"key": "wd", "value": {"float_val": 0.1}}, seq=1)
            )

            _, rows = store.query("SELECT COUNT(*) FROM params")
            assert rows[0][0] == 2


class TestIdempotency:
    def test_duplicate_seq_ignored(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env = _env("value", {"key": "loss", "value": 0.5, "step": None})
            store.insert_event(env)
            store.insert_event(env)

            _, rows = store.query("SELECT COUNT(*) FROM tracked_values")
            assert rows[0][0] == 1

    def test_different_seq_accepted(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            env0 = _env("value", {"key": "loss", "value": 0.5, "step": None}, seq=0)
            env1 = _env("value", {"key": "loss", "value": 0.3, "step": None}, seq=1)
            store.insert_event(env0)
            store.insert_event(env1)

            _, rows = store.query(
                "SELECT seq, scalar_val FROM tracked_values ORDER BY seq"
            )
            assert rows == [(0, 0.5), (1, 0.3)]


class TestMigration:
    def test_old_schema_dropped_on_open(self, tmp_path):
        db = tmp_path / "old.sqlite"
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE metrics (project TEXT, study_name TEXT, "
            "trial_id INTEGER, timestamp_ns INTEGER, seq INTEGER, "
            "key TEXT, value REAL, step INTEGER) STRICT"
        )
        con.execute('INSERT INTO metrics VALUES ("p","s",0,1,0,"loss",0.5,10)')
        con.execute(
            "CREATE TABLE results (project TEXT, study_name TEXT, trial_id INTEGER, "
            "timestamp_ns INTEGER, seq INTEGER, key TEXT, value TEXT) STRICT"
        )
        con.commit()
        con.close()

        with Store(db) as store:
            _, tables = store.query("SELECT name FROM sqlite_master WHERE type='table'")
            names = {r[0] for r in tables}
            assert "metrics" not in names
            assert "results" not in names
            assert "tracked_values" in names
            _, version = store.query("PRAGMA user_version")
            assert version[0][0] == 2

    def test_fresh_db_gets_version_two(self, tmp_path):
        with Store(tmp_path / "fresh.sqlite") as store:
            _, version = store.query("PRAGMA user_version")
            assert version[0][0] == 2


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
                    "value",
                    {"key": "loss", "value": 0.5, "step": 10, "context": "{}"},
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
            _, rows = store.query("SELECT COUNT(*) FROM tracked_values")
            assert rows[0][0] == 1

            _, rows = store.query(
                "SELECT trial_id, float_val FROM params"
                " WHERE key = 'lr' ORDER BY trial_id"
            )
            assert rows == [(0, 0.01), (1, 0.1)]


class TestSecondaryIndexes:
    def test_indexes_exist(self, tmp_path):
        with Store(tmp_path / "test.sqlite") as store:
            _, rows = store.query(
                "SELECT name FROM sqlite_master WHERE type='index'"
                " AND tbl_name='tracked_values'"
            )
            names = {r[0] for r in rows}
            assert "idx_values_study_key" in names
            assert "idx_values_study_key_step" in names
