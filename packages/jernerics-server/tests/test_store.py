import hashlib
import re
import shutil
import sqlite3
from pathlib import Path

import pytest
from jernerics_server import store as store_module
from jernerics_server.store import (
    FutureSchemaError,
    LegacyStoreError,
    Store,
    StoreError,
    archive_v2,
)

TABLES = {
    "sweeps",
    "submissions",
    "submission_jobs",
    "trials",
    "trial_params",
    "executions",
    "execution_progress",
    "tracked_values",
    "artifacts",
    "artifact_blobs",
    "reconciliation_conflicts",
}

INDEXES = {
    "idx_sweeps_recency",
    "idx_trials_state",
    "idx_trials_retry_root",
    "idx_executions_heartbeat",
    "idx_executions_outcome",
    "idx_artifacts_exec_key",
    "idx_executions_trial",
    "idx_submissions_sweep",
}


def _write(path: Path, sql: str, params: tuple = ()) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.execute(sql, params)
    con.commit()
    con.close()


def _seed_graph(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(
        """
        INSERT INTO sweeps (sweep_id, project, name, state, created_ns, updated_ns)
        VALUES ('sw', 'p', 'n', 'running', 1, 2);
        INSERT INTO submissions
        (submission_id, sweep_id, backend, state, created_ns, updated_ns)
        VALUES ('sub', 'sw', 'slurm', 'submitted', 1, 2);
        INSERT INTO submission_jobs
        (job_id, submission_id, scheduler_job_id, state, updated_ns)
        VALUES ('job', 'sub', '123', 'pending', 2);
        INSERT INTO trials (trial_id, sweep_id, number, state, retry_of_trial_id,
        retry_root_trial_id, retry_index, created_ns, updated_ns)
        VALUES ('t1', 'sw', 1, 'running', NULL, 't1', 0, 1, 2);
        INSERT INTO trial_params (trial_id, kind, key, value_json, updated_ns)
        VALUES ('t1', 'sampled', 'lr', '0.1', 2);
        INSERT INTO executions (execution_id, trial_id, hostname, started_ns,
        ended_ns, last_heartbeat_ns, last_observation_ns, outcome, exit_code,
        created_ns, updated_ns)
        VALUES ('e1', 't1', 'node7', 10, 20, 15, 16, 'success', 0, 10, 20);
        INSERT INTO execution_progress
        (execution_id, current, total, unit, updated_ns)
        VALUES ('e1', 3, 10, 'epoch', 16);
        INSERT INTO tracked_values (execution_id, key, step, value_type,
        scalar_val, text_val, context, recorded_ns)
        VALUES ('e1', 'loss', 0, 'scalar', 0.5, NULL, '{}', 16);
        INSERT INTO artifacts (artifact_id, trial_id, execution_id, key, filename,
        content_type, size_bytes, sha256, declared_ns, received_ns)
        VALUES ('a1', 't1', 'e1', 'model', 'model.pt',
        'application/octet-stream', 3, NULL, 16, 17);
        INSERT INTO artifact_blobs
        (artifact_id, rel_path, sha256, size_bytes, received_ns)
        VALUES ('a1', 'p/n/1/model.pt', '00', 3, 17);
        INSERT INTO reconciliation_conflicts (trial_id, kind, detail, detected_ns)
        VALUES ('t1', 'duplicate_number', '{}', 18);
        """
    )
    con.commit()
    con.close()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "store.sqlite"
    Store(path).close()
    _seed_graph(path)
    return path


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


class TestInit:
    def test_fresh_store_creates_current_schema(self, tmp_path):
        path = tmp_path / "store.sqlite"
        with Store(path) as store:
            assert store.query("PRAGMA user_version")[1] == [(6,)]
            con = sqlite3.connect(path)
            assert _table_names(con) - {"sqlite_sequence"} == TABLES
            con.close()
            store.verify()

    def test_wal_mode_active(self, tmp_path):
        with Store(tmp_path / "store.sqlite") as store:
            assert store.query("PRAGMA journal_mode")[1] == [("wal",)]

    def test_reopen_existing_store_is_noop_and_keeps_data(self, db_path):
        with Store(db_path) as store:
            assert store.query("PRAGMA user_version")[1] == [(6,)]
            assert store.query("SELECT trial_id FROM trials")[1] == [("t1",)]
            assert store.query("SELECT COUNT(*) FROM tracked_values")[1] == [(1,)]


class TestIndexes:
    def test_exact_index_set(self, tmp_path):
        with Store(tmp_path / "store.sqlite") as store:
            _, rows = store.query("SELECT name FROM sqlite_master WHERE type = 'index'")
        explicit = {name for (name,) in rows if not name.startswith("sqlite_")}
        assert explicit == INDEXES


class TestConstraints:
    def test_foreign_key_violation_rejected(self, db_path):
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            con.execute(
                "INSERT INTO executions (execution_id, trial_id, hostname,"
                " started_ns, created_ns, updated_ns)"
                " VALUES ('e2', 'ghost', 'h', 1, 1, 1)"
            )
        con.close()

    def test_retry_root_self_reference_accepted(self, db_path):
        _write(
            db_path,
            "INSERT INTO trials (trial_id, sweep_id, number, state,"
            " retry_of_trial_id, retry_root_trial_id, retry_index,"
            " created_ns, updated_ns)"
            " VALUES ('t2', 'sw', 2, 'waiting', 't1', 't1', 1, 3, 3)",
        )
        with Store(db_path) as store:
            assert store.query(
                "SELECT retry_root_trial_id, retry_index FROM trials"
                " WHERE trial_id = 't2'"
            )[1] == [("t1", 1)]

    def test_duplicate_tracked_values_rejected(self, db_path):
        with pytest.raises(sqlite3.IntegrityError):
            _write(
                db_path,
                "INSERT INTO tracked_values (execution_id, key, step,"
                " value_type, scalar_val, text_val, context, recorded_ns)"
                " VALUES ('e1', 'loss', 0, 'scalar', 0.6, NULL, '{}', 17)",
            )

    def test_same_key_different_step_accepted(self, db_path):
        _write(
            db_path,
            "INSERT INTO tracked_values (execution_id, key, step,"
            " value_type, scalar_val, text_val, context, recorded_ns)"
            " VALUES ('e1', 'loss', 1, 'scalar', 0.4, NULL, '{}', 17)",
        )
        with Store(db_path) as store:
            assert store.query("SELECT step FROM tracked_values ORDER BY step")[1] == [
                (0,),
                (1,),
            ]

    def test_duplicate_trial_params_rejected(self, db_path):
        with pytest.raises(sqlite3.IntegrityError):
            _write(
                db_path,
                "INSERT INTO trial_params (trial_id, kind, key, value_json,"
                " updated_ns) VALUES ('t1', 'sampled', 'lr', '0.2', 3)",
            )

    def test_param_kind_manual_distinct_from_sampled(self, db_path):
        _write(
            db_path,
            "INSERT INTO trial_params (trial_id, kind, key, value_json,"
            " updated_ns) VALUES ('t1', 'manual', 'lr', '0.9', 3)",
        )
        with Store(db_path) as store:
            assert store.query(
                "SELECT kind FROM trial_params WHERE key = 'lr' ORDER BY kind"
            )[1] == [("manual",), ("sampled",)]

    @pytest.mark.parametrize(
        "sql",
        [
            # trial state outside the TrialState enum
            "INSERT INTO trials (trial_id, sweep_id, number, state,"
            " retry_root_trial_id, retry_index, created_ns, updated_ns)"
            " VALUES ('t9', 'sw', 9, 'bogus', 't9', 0, 1, 1)",
            # submission state outside the SubmissionState enum
            "INSERT INTO submissions (submission_id, sweep_id, backend,"
            " state, created_ns, updated_ns)"
            " VALUES ('sub9', 'sw', 'slurm', 'bogus', 1, 1)",
            # job state outside the SubmissionState enum
            "INSERT INTO submission_jobs (job_id, submission_id,"
            " scheduler_job_id, state, updated_ns)"
            " VALUES ('job9', 'sub', '999', 'bogus', 1)",
            # execution outcome outside the ExecutionOutcome enum
            "INSERT INTO executions (execution_id, trial_id, hostname,"
            " started_ns, outcome, created_ns, updated_ns)"
            " VALUES ('e9', 't1', 'h', 1, 'exploded', 1, 1)",
            # failure kind outside the FailureKind enum
            "INSERT INTO executions (execution_id, trial_id, hostname,"
            " started_ns, failure_kind, created_ns, updated_ns)"
            " VALUES ('e9', 't1', 'h', 1, 'segfault', 1, 1)",
            # scalar without a scalar value
            "INSERT INTO tracked_values (execution_id, key, step, value_type,"
            " scalar_val, text_val, context, recorded_ns)"
            " VALUES ('e1', 'acc', 0, 'scalar', NULL, NULL, '{}', 1)",
            # json without a text value
            "INSERT INTO tracked_values (execution_id, key, step, value_type,"
            " scalar_val, text_val, context, recorded_ns)"
            " VALUES ('e1', 'acc', 0, 'json', NULL, NULL, '{}', 1)",
            # json carrying a scalar value
            "INSERT INTO tracked_values (execution_id, key, step, value_type,"
            " scalar_val, text_val, context, recorded_ns)"
            " VALUES ('e1', 'acc', 0, 'json', 0.5, '\"x\"', '{}', 1)",
            # unknown value_type entirely
            "INSERT INTO tracked_values (execution_id, key, step, value_type,"
            " scalar_val, text_val, context, recorded_ns)"
            " VALUES ('e1', 'acc', 0, 'text', NULL, 'x', '{}', 1)",
            # non-JSON context
            "INSERT INTO tracked_values (execution_id, key, step, value_type,"
            " scalar_val, text_val, context, recorded_ns)"
            " VALUES ('e1', 'acc', 0, 'scalar', 1.0, NULL, 'not json', 1)",
            # non-JSON param value
            "INSERT INTO trial_params (trial_id, kind, key, value_json,"
            " updated_ns) VALUES ('t1', 'sampled', 'wd', '{oops', 3)",
            # param kind outside ('sampled', 'manual')
            "INSERT INTO trial_params (trial_id, kind, key, value_json,"
            " updated_ns) VALUES ('t1', 'guessed', 'wd', '0.1', 3)",
            # negative retry index
            "INSERT INTO trials (trial_id, sweep_id, number, state,"
            " retry_root_trial_id, retry_index, created_ns, updated_ns)"
            " VALUES ('t9', 'sw', 9, 'waiting', 't1', -1, 1, 1)",
            # negative step
            "INSERT INTO tracked_values (execution_id, key, step, value_type,"
            " scalar_val, text_val, context, recorded_ns)"
            " VALUES ('e1', 'acc', -1, 'scalar', 1.0, NULL, '{}', 1)",
            # failure summary past the 2000-char bound
            "INSERT INTO executions (execution_id, trial_id, hostname,"
            " started_ns, failure_summary, created_ns, updated_ns)"
            " VALUES ('e9', 't1', 'h', 1, '" + "x" * 2001 + "', 1, 1)",
            # sha256 that is not 64 hex chars
            "INSERT INTO artifacts (artifact_id, trial_id, execution_id, key,"
            " filename, content_type, size_bytes, sha256, declared_ns)"
            " VALUES ('a9', 't1', 'e1', 'm', 'm.pt', 'x', 3, '00', 1)",
            # negative artifact size
            "INSERT INTO artifacts (artifact_id, trial_id, execution_id, key,"
            " filename, content_type, size_bytes, sha256, declared_ns)"
            " VALUES ('a9', 't1', 'e1', 'm', 'm.pt', 'x', -1, NULL, 1)",
            # non-positive progress total
            "INSERT INTO execution_progress (execution_id, current, total,"
            " unit, updated_ns) VALUES ('e1', 0, 0, 'epoch', 1)",
            # non-JSON conflict detail
            "INSERT INTO reconciliation_conflicts (trial_id, kind, detail,"
            " detected_ns) VALUES ('t1', 'k', 'nope', 1)",
        ],
    )
    def test_check_constraints_reject_bad_rows(self, db_path, sql):
        with pytest.raises(sqlite3.IntegrityError):
            _write(db_path, sql)

    def test_failure_summary_at_bound_accepted(self, db_path):
        _write(
            db_path,
            "INSERT INTO executions (execution_id, trial_id, hostname,"
            " started_ns, failure_kind, failure_summary, created_ns,"
            " updated_ns) VALUES ('e9', 't1', 'h', 1, 'oom', '"
            + "x" * 2000
            + "', 1, 1)",
        )
        with Store(db_path) as store:
            assert store.query(
                "SELECT length(failure_summary) FROM executions"
                " WHERE execution_id = 'e9'"
            )[1] == [(2000,)]


class TestBackup:
    def test_backup_restores_identical_contents(self, tmp_path):
        src = tmp_path / "store.sqlite"
        dest = tmp_path / "backup.sqlite"
        with Store(src) as store:
            _seed_graph(src)
            store.backup_to(dest)
            with Store(dest) as restored:
                restored.verify()
                for table in sorted(TABLES):
                    assert (
                        restored.query(f"SELECT * FROM {table}")[1]
                        == store.query(f"SELECT * FROM {table}")[1]
                    )
        assert list(tmp_path.glob(".backup.sqlite.*")) == []

    def test_backup_repeatedly_overwrites_dest(self, tmp_path):
        src = tmp_path / "store.sqlite"
        dest = tmp_path / "backup.sqlite"
        with Store(src) as store:
            _seed_graph(src)
            store.backup_to(dest)
            store.backup_to(dest)
        with Store(dest) as restored:
            assert restored.query("SELECT COUNT(*) FROM trials")[1] == [(1,)]


class TestVerify:
    def test_passes_on_healthy_store(self, db_path):
        with Store(db_path) as store:
            store.verify()

    def test_detects_foreign_key_violations(self, db_path):
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute(
            "INSERT INTO executions (execution_id, trial_id, hostname,"
            " started_ns, created_ns, updated_ns)"
            " VALUES ('e9', 'ghost', 'h', 1, 1, 1)"
        )
        con.commit()
        con.close()
        with (
            Store(db_path) as store,
            pytest.raises(StoreError, match="foreign_key_check"),
        ):
            store.verify()


class TestLegacyRefusal:
    def test_user_version_2_refused(self, tmp_path):
        path = tmp_path / "store.sqlite"
        con = sqlite3.connect(path)
        con.execute("PRAGMA user_version=2")
        con.close()
        with pytest.raises(LegacyStoreError, match="archive_v2"):
            Store(path)

    def test_user_version_1_refused_on_empty_file(self, tmp_path):
        path = tmp_path / "store.sqlite"
        con = sqlite3.connect(path)
        con.execute("PRAGMA user_version=1")
        con.close()
        with pytest.raises(LegacyStoreError, match="fresh"):
            Store(path)

    def test_v2_tables_refused_despite_user_version_0(self, tmp_path):
        path = tmp_path / "store.sqlite"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE sweep_meta (x INT)")
        con.execute("CREATE TABLE trial_end (x INT)")
        con.execute("CREATE TABLE params (x INT)")
        con.commit()
        con.close()
        with pytest.raises(LegacyStoreError, match="sweep_meta"):
            Store(path)

    def test_refusal_leaves_file_untouched(self, tmp_path):
        path = tmp_path / "store.sqlite"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE params (k TEXT)")
        con.execute("INSERT INTO params VALUES ('lr')")
        con.commit()
        con.close()
        before = path.read_bytes()
        with pytest.raises(LegacyStoreError):
            Store(path)
        assert path.read_bytes() == before
        con = sqlite3.connect(path)
        assert con.execute("SELECT k FROM params").fetchall() == [("lr",)]
        con.close()


class TestFutureSchema:
    def test_user_version_beyond_supported_refused(self, tmp_path):
        path = tmp_path / "store.sqlite"
        con = sqlite3.connect(path)
        con.execute("PRAGMA user_version=7")
        con.close()
        with pytest.raises(FutureSchemaError, match="version 7"):
            Store(path)


class TestMigrationV3ToV4:
    def _make_v3_file(self, path: Path) -> None:
        con = sqlite3.connect(path)
        store_module._MIGRATIONS[3](con)
        con.execute("PRAGMA user_version=3")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(
            """
            INSERT INTO sweeps (sweep_id, project, name, state, created_ns, updated_ns)
            VALUES ('sw', 'p', 'n', 'running', 1, 2);
            INSERT INTO submissions
            (submission_id, sweep_id, backend, state, created_ns, updated_ns)
            VALUES ('sub', 'sw', 'slurm', 'submitted', 1, 2);
            INSERT INTO submission_jobs
            (job_id, submission_id, scheduler_job_id, state, updated_ns)
            VALUES ('job', 'sub', '123', 'submitted', 2);
            """
        )
        con.commit()
        con.close()

    def test_v3_file_upgrades_in_place(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v3_file(path)

        with Store(path) as store:
            assert store.query("PRAGMA user_version")[1] == [(6,)]
            submission_cols = {
                row[1] for row in store.query("PRAGMA table_info(submissions)")[1]
            }
            assert {
                "submitted_ns",
                "expected_trials",
                "git_hash",
                "config_source",
            } <= submission_cols
            job_cols = {
                row[1] for row in store.query("PRAGMA table_info(submission_jobs)")[1]
            }
            store.verify()

    def test_v3_data_survives_upgrade(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v3_file(path)

        with Store(path) as store:
            assert store.query("SELECT submission_id, backend, state FROM submissions")[
                1
            ] == [("sub", "slurm", "submitted")]
            assert store.query(
                "SELECT job_id, scheduler_job_id, role FROM submission_jobs"
            )[1] == [("job", "123", None)]

    def test_new_columns_accept_writes_after_upgrade(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v3_file(path)

        with Store(path) as store:
            store._con.execute(
                "UPDATE submissions SET submitted_ns = 5, expected_trials = 8, "
                "git_hash = 'abc', config_source = 'config.py', "
                "updated_ns = 5 WHERE submission_id = 'sub'"
            )
            store._con.execute(
                "UPDATE submission_jobs SET role = 'trials' WHERE job_id = 'job'"
            )
            assert store.query(
                "SELECT submitted_ns, expected_trials, git_hash, config_source "
                "FROM submissions"
            )[1] == [(5, 8, "abc", "config.py")]
            assert store.query("SELECT role FROM submission_jobs")[1] == [("trials",)]


class TestMigrationV4ToV5:
    def _make_v4_file(self, path: Path) -> None:
        con = sqlite3.connect(path)
        store_module._MIGRATIONS[3](con)
        store_module._MIGRATIONS[4](con)
        con.execute("PRAGMA user_version=4")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(
            """
            INSERT INTO sweeps (sweep_id, project, name, state, created_ns, updated_ns)
            VALUES ('sw', 'p', 'n', 'running', 1, 2);
            INSERT INTO trials (trial_id, sweep_id, number, state,
            retry_root_trial_id, retry_index, created_ns, updated_ns)
            VALUES ('t1', 'sw', 0, 'completed', 't1', 0, 1, 2);
            INSERT INTO trial_params (trial_id, kind, key, value_json, updated_ns)
            VALUES ('t1', 'sampled', 'lr', '0.1', 2);
            """
        )
        con.commit()
        con.close()

    def test_v4_file_upgrades_in_place(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v4_file(path)

        with Store(path) as store:
            assert store.query("PRAGMA user_version")[1] == [(6,)]
            trial_cols = {row[1] for row in store.query("PRAGMA table_info(trials)")[1]}
            assert {"objective", "distributions_json", "attrs_json"} <= trial_cols
            store.verify()

    def test_v4_data_survives_upgrade(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v4_file(path)

        with Store(path) as store:
            assert store.query("SELECT trial_id, number, state FROM trials")[1] == [
                ("t1", 0, "completed")
            ]
            assert store.query("SELECT key, value_json FROM trial_params")[1] == [
                ("lr", "0.1")
            ]

    def test_new_columns_accept_writes_after_upgrade(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v4_file(path)

        with Store(path) as store:
            store._con.execute(
                "UPDATE trials SET objective = 0.5, distributions_json = '{}', "
                "attrs_json = '{}', updated_ns = 3 WHERE trial_id = 't1'"
            )
            assert store.query(
                "SELECT objective, distributions_json, attrs_json FROM trials"
            )[1] == [(0.5, "{}", "{}")]


class TestMigrationV5ToV6:
    def _make_v5_file(self, path: Path) -> None:
        con = sqlite3.connect(path)
        for version in (3, 4, 5):
            store_module._MIGRATIONS[version](con)
        con.execute("PRAGMA user_version=5")
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(
            """
            INSERT INTO sweeps (sweep_id, project, name, state, created_ns, updated_ns)
            VALUES ('sw', 'p', 'n', 'running', 1, 2);
            INSERT INTO trials (trial_id, sweep_id, number, state,
            retry_root_trial_id, retry_index, created_ns, updated_ns)
            VALUES ('t1', 'sw', 0, 'completed', 't1', 0, 1, 2);
            INSERT INTO artifacts (artifact_id, trial_id, execution_id, key,
            filename, content_type, size_bytes, sha256, declared_ns)
            VALUES ('a1', 't1', NULL, 'model', 'model.pt',
            'application/octet-stream', 3, NULL, 5);
            """
        )
        con.commit()
        con.close()

    def test_v5_file_upgrades_in_place(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v5_file(path)

        with Store(path) as store:
            assert store.query("PRAGMA user_version")[1] == [(6,)]
            artifact_cols = {
                row[1] for row in store.query("PRAGMA table_info(artifacts)")[1]
            }
            assert {"context_json", "source"} <= artifact_cols
            store.verify()

    def test_v5_artifacts_get_source_default_and_keep_data(self, tmp_path):
        path = tmp_path / "store.sqlite"
        self._make_v5_file(path)

        with Store(path) as store:
            assert store.query(
                "SELECT artifact_id, key, context_json, source FROM artifacts"
            )[1] == [("a1", "model", None, "user")]


class TestMigrationAtomicity:
    def test_failing_migration_rolls_back_completely(self, tmp_path, monkeypatch):
        def failing(con: sqlite3.Connection) -> None:
            con.execute(store_module._TABLE_STATEMENTS[0])
            raise RuntimeError("boom")

        monkeypatch.setitem(store_module._MIGRATIONS, 3, failing)
        path = tmp_path / "store.sqlite"
        with pytest.raises(RuntimeError, match="boom"):
            Store(path)
        con = sqlite3.connect(path)
        assert _table_names(con) == set()
        assert con.execute("PRAGMA user_version").fetchone()[0] == 0
        con.close()

    def test_fresh_store_usable_after_failed_attempt(self, tmp_path, monkeypatch):
        def failing(con: sqlite3.Connection) -> None:
            raise RuntimeError("boom")

        monkeypatch.setitem(store_module._MIGRATIONS, 3, failing)
        path = tmp_path / "store.sqlite"
        with pytest.raises(RuntimeError):
            Store(path)
        monkeypatch.undo()
        with Store(path) as store:
            store.verify()
            assert store.query("PRAGMA user_version")[1] == [(6,)]


def _make_v2_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE params (project TEXT, key TEXT)")
    con.execute("INSERT INTO params VALUES ('p', 'lr')")
    con.commit()
    con.close()


def _make_artifacts(root: Path) -> None:
    (root / "p" / "s").mkdir(parents=True)
    (root / "p" / "s" / "ckpt.bin").write_bytes(b"model-bytes")
    (root / "note.txt").write_text("hi")


class TestArchiveV2:
    def test_archives_database_and_artifacts_with_manifest(self, tmp_path):
        db = tmp_path / "old.sqlite"
        _make_v2_db(db)
        artifacts = tmp_path / "artifacts"
        _make_artifacts(artifacts)
        dest = tmp_path / "archives"

        archive = archive_v2(db, artifacts, dest)

        assert archive.parent == dest
        assert re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z", archive.name)
        files = sorted(
            p.relative_to(archive).as_posix() for p in archive.rglob("*") if p.is_file()
        )
        assert files == [
            "SHA256SUMS",
            "artifacts/note.txt",
            "artifacts/p/s/ckpt.bin",
            "old.sqlite",
        ]
        con = sqlite3.connect(archive / "old.sqlite")
        assert con.execute("SELECT key FROM params").fetchall() == [("lr",)]
        con.close()
        for line in (archive / "SHA256SUMS").read_text().splitlines():
            digest, rel = line.split("  ", 1)
            assert hashlib.sha256((archive / rel).read_bytes()).hexdigest() == digest
        con = sqlite3.connect(db)
        assert con.execute("SELECT COUNT(*) FROM params").fetchone() == (1,)
        con.close()
        assert (artifacts / "p" / "s" / "ckpt.bin").read_bytes() == b"model-bytes"

    def test_missing_artifacts_root_skipped(self, tmp_path):
        db = tmp_path / "old.sqlite"
        _make_v2_db(db)
        archive = archive_v2(db, tmp_path / "nope", tmp_path / "archives")
        assert (archive / "old.sqlite").exists()
        assert not (archive / "nope").exists()

    def test_corrupt_database_falls_back_to_plain_copy(self, tmp_path):
        db = tmp_path / "old.sqlite"
        payload = b"this is not a sqlite database" * 64
        db.write_bytes(payload)
        (tmp_path / "old.sqlite-wal").write_bytes(b"wal-sidecar")
        archive = archive_v2(db, tmp_path / "nope", tmp_path / "archives")
        assert (archive / "old.sqlite").read_bytes() == payload
        assert (archive / "old.sqlite-wal").read_bytes() == b"wal-sidecar"

    def test_missing_database_raises_and_leaves_no_archive(self, tmp_path):
        with pytest.raises(StoreError):
            archive_v2(
                tmp_path / "absent.sqlite",
                tmp_path / "nope",
                tmp_path / "archives",
            )
        assert (
            not (tmp_path / "archives").exists()
            or list((tmp_path / "archives").iterdir()) == []
        )

    def test_failure_mid_way_leaves_no_partial_archive(self, tmp_path, monkeypatch):
        db = tmp_path / "old.sqlite"
        _make_v2_db(db)
        artifacts = tmp_path / "artifacts"
        _make_artifacts(artifacts)
        dest = tmp_path / "archives"
        dest.mkdir()

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(shutil, "copytree", boom)
        with pytest.raises(StoreError, match="disk full"):
            archive_v2(db, artifacts, dest)

        assert list(dest.iterdir()) == []
        con = sqlite3.connect(db)
        assert con.execute("SELECT COUNT(*) FROM params").fetchone() == (1,)
        con.close()

    def test_two_archives_get_distinct_directories(self, tmp_path):
        db = tmp_path / "old.sqlite"
        _make_v2_db(db)
        dest = tmp_path / "archives"
        first = archive_v2(db, tmp_path / "nope", dest)
        second = archive_v2(db, tmp_path / "nope", dest)
        assert first != second
        assert first.is_dir() and second.is_dir()
