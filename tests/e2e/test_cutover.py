"""The full v2 → v3 cutover sequence against synthetic v2 data.

Proves the recipe documented in ``docs/tracking-v3-cutover.md``:
archive the legacy database and artifacts with a checksum manifest,
verify the manifest, start v3 on a fresh path (empty schema v6), and
confirm the v3 store refuses to open any v2 file it is ever pointed at.
"""

import hashlib
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jernerics_server.http import create_app
from jernerics_server.store import (
    SCHEMA_VERSION,
    LegacyStoreError,
    Store,
    archive_v2,
)


def _make_v2_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        PRAGMA user_version=2;
        CREATE TABLE sweep_meta (
            project TEXT, study_name TEXT, n_trials INTEGER, created_at TEXT
        );
        CREATE TABLE trial_end (
            study_name TEXT, trial_id INTEGER, exit_code INTEGER, ended_at TEXT
        );
        CREATE TABLE params (
            study_name TEXT, trial_id INTEGER, key TEXT, value TEXT
        );
        INSERT INTO sweep_meta VALUES ('proj', 'legacy-sweep', 3, '2026-01-01');
        INSERT INTO trial_end VALUES ('legacy-sweep', 0, 0, '2026-01-02');
        INSERT INTO params VALUES ('legacy-sweep', 0, 'lr', '0.1');
        """
    )
    con.commit()
    con.close()


def _make_artifacts(root: Path) -> None:
    (root / "legacy-sweep" / "0").mkdir(parents=True)
    (root / "legacy-sweep" / "0" / "model.pt").write_bytes(b"model-bytes")
    (root / "legacy-sweep" / "0" / "summary.json").write_text('{"loss": 0.5}')


def test_v2_archive_then_v3_startup_refuses_legacy(tmp_path: Path) -> None:
    live_db = tmp_path / "db.sqlite"
    artifacts_root = tmp_path / "artifacts"
    _make_v2_db(live_db)
    _make_artifacts(artifacts_root)

    archive = archive_v2(live_db, artifacts_root, tmp_path / "archive")

    manifest_lines = (archive / "SHA256SUMS").read_text().splitlines()
    assert sorted(line.split("  ", 1)[1] for line in manifest_lines) == [
        "artifacts/legacy-sweep/0/model.pt",
        "artifacts/legacy-sweep/0/summary.json",
        "db.sqlite",
    ]
    for line in manifest_lines:
        digest, rel = line.split("  ", 1)
        assert hashlib.sha256((archive / rel).read_bytes()).hexdigest() == digest

    archived_db = archive / "db.sqlite"
    con = sqlite3.connect(archived_db)
    assert con.execute("PRAGMA user_version").fetchone() == (2,)
    assert con.execute("SELECT key FROM params").fetchall() == [("lr",)]
    con.close()

    live_db.rename(tmp_path / "db.sqlite.v2-retired")
    artifacts_root.rename(tmp_path / "artifacts.v2-retired")

    fresh = tmp_path / "db.sqlite"
    with Store(fresh) as store:
        assert store.query("PRAGMA user_version")[1] == [(SCHEMA_VERSION,)]
        assert store.query("SELECT COUNT(*) FROM sweeps")[1] == [(0,)]
        store.verify()

    app = create_app(Store(fresh), artifacts_root=tmp_path / "artifacts")
    with TestClient(app) as client:
        assert client.get("/api/health").json() == {"ok": True}

    for legacy in (tmp_path / "db.sqlite.v2-retired", archived_db):
        with pytest.raises(LegacyStoreError, match="legacy"):
            Store(legacy)
