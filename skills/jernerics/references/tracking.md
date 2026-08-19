# Tracking

## Tracking server

A single HTTP process receives trial events and serves them. It exposes:

- `POST /ingest` — ingest one JSONL event envelope (a metric, param, result, artifact, or trial_end)
- `POST /query` — run read-only SQL against the SQLite store, return JSON rows
- `GET /api/health` — liveness
- `GET /artifact/{project}/{study}/{trial_id}/{key}` — download an artifact file
- `POST /artifact/{project}/{study}/{trial_id}/{key}` — upload an artifact file (raw body)

The server address is set via:

```bash
export JERNERICS_TRACKING_SERVER="http://host:port"
```

Or in `pyproject.toml`:

```toml
[tool.jernerics]
tracking_server = "http://host:port"
```

When configured, each trial ships events live to the server over HTTP (a ship
client tails the local JSONL file and POSTs each envelope to `/ingest`). If the
server is unreachable, events are still written locally and replayed later.

## Local tracking data

Trial events are written as JSONL (one JSON object per line) to the local
tracking directory:

```
~/.cache/jernerics/<project>/tracking/<study>/events/*.jsonl
```

The format is human-readable — inspect a file directly to debug a trial.

## Replay

`jernerics tracking replay` ships tracking data to the server, in one of two
modes:

```bash
jernerics tracking replay                       # local cache → server
jernerics tracking replay --study <study_name>
jernerics tracking replay --dry-run             # compare against server, send nothing
jernerics tracking replay --json

jernerics tracking replay -b <name>             # pull backend cache → server
jernerics tracking replay -b <name> --study <study_name>
```

Without `--backend`, the local cache (`cache_dir()/tracking`) is replayed
(`--study`, `--tracking-dir`, `--server`, `--dry-run`, `--json` all apply).
With `--backend NAME`, the backend's remote tracking cache is pulled first,
then shipped (`--study` applies; the local-only options are rejected with
an error rather than silently ignored).

The post-hook pipeline also runs replay automatically after a sweep completes.
Ingest is idempotent (`INSERT OR IGNORE` on a unique seq), so live shipping and
replay overlapping is safe.

## Artifact storage

Artifacts logged via `tracker.log_artifact()` are uploaded to the tracking
server's disk over HTTP (`POST /artifact/...`) and served back the same way
(`GET /artifact/...`). No external object storage (S3/MinIO) is required —
artifacts live on the server's disk, configured via `--artifacts-dir`.

The same `JERNERICS_API_KEY` (bearer token) authenticates both event ingest
and artifact upload/download. `jernerics run` forwards `JERNERICS_API_KEY` to
the container.

Artifact manifests track byte offsets so only new artifacts are uploaded on
each sync.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `JERNERICS_TRACKING_SERVER` | Tracking HTTP server base URL (`http://host:port`) |
| `JERNERICS_API_KEY` | Optional bearer token; must match server and client |
