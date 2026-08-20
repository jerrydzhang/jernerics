# Tracking v3 cutover

How to move a deployment from the legacy (v1/v2) tracking server to the
v3 store. v3 never migrates a legacy database in place and never drops
tracking data: the old database and its artifacts are archived with
checksums, the originals are moved aside, and the server starts on a
fresh path with an empty schema.

## When you need this

The v3 store refuses to open a legacy database. If the server log shows
``LegacyStoreError`` ("This is a legacy (v1/v2) tracking database"), the
store path holds v2 data and the sequence below is the only supported
path forward.

## The sequence

Run every step on the machine that owns the store; the examples assume
the server runs under the NixOS module with the database at
``/var/lib/jernerics/db.sqlite`` and artifacts beside it.

### 1. Stop the server

Stop any process still using the database so the archive catches a
quiescent file:

```bash
systemctl stop jernerics-tracking
```

### 2. Archive the v2 database and artifacts

```bash
python -c "
from pathlib import Path
from jernerics_server.store import archive_v2
archive_v2(
    Path('/var/lib/jernerics/db.sqlite'),
    Path('/var/lib/jernerics/artifacts'),
    Path('/var/lib/jernerics/archive'),
)
"
```

``archive_v2`` takes a best-effort online backup of the SQLite file
(plain byte copy, including WAL sidecars, when the file is not
openable), copies the artifact root recursively when present, and writes
everything into ``archive/<timestamp>/`` via a temp directory and an
atomic rename. The archive carries a ``SHA256SUMS`` manifest listing
every archived file. The sources are never written to, and a failure
leaves no partial archive behind.

### 3. Verify the manifest

Every line of ``SHA256SUMS`` must match the file it names:

```bash
cd /var/lib/jernerics/archive/<timestamp> && sha256sum -c SHA256SUMS
```

### 4. Move the originals aside

Keep the archived originals out of the store path so the server cannot
reopen them by accident. ``archive_v2`` copies (never moves), so rename
or remove the source files only after the checksum check passes:

```bash
mv /var/lib/jernerics/db.sqlite /var/lib/jernerics/db.sqlite.v2-retired
mv /var/lib/jernerics/artifacts /var/lib/jernerics/artifacts.v2-retired
```

The archive directory is the durable record; the retired copies can be
deleted once you trust it.

### 5. Start v3 on the fresh path

```bash
systemctl start jernerics-tracking
```

The server creates a new empty v3 store (schema version 6) at the same
path and an empty artifacts directory beside it.

### 6. Verify

Point the server at itself once to confirm health, then run the store
integrity check:

```bash
curl -s http://127.0.0.1:8000/api/health   # {"ok": true}

python -c "
from jernerics_server.store import Store
store = Store('/var/lib/jernerics/db.sqlite')
store.verify()
print('integrity ok, schema', store.query('PRAGMA user_version')[1][0][0])
store.close()
"
```

``Store.verify()`` runs ``PRAGMA integrity_check`` and
``PRAGMA foreign_key_check``; both must come back clean.

### Refusal behavior

If the v3 server is ever pointed at the archived v2 file again — say the
retired copy is restored to the store path — ``Store`` raises
``LegacyStoreError`` and refuses to start. The refusal detects both
``PRAGMA user_version`` of 1 or 2 and v2 tables (``sweep_meta``,
``trial_end``, ``params``) regardless of the version stamp. Nothing is
migrated or overwritten; the file is left untouched.

The full sequence, including the manifest check and the refusal, is
covered end-to-end by ``tests/e2e/test_cutover.py`` against synthetic
v2 data.
