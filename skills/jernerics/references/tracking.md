# Tracking

## Tracking server

A single HTTP process (`python -m jernerics_server`) receives tracked
events and serves them. One SQLite store (schema v6) plus an artifacts
directory on its own disk. Endpoints:

- `POST /ingest` — one batch of tagged v3 events (JSONL lines: sweep,
  submission, job, trial snapshots; execution lifecycle; params, values,
  progress; artifact declarations). Idempotent per event id: replays
  and live-stream overlaps land as duplicates, never double-apply.
- `PUT /artifact/{artifact_id}` — upload one declared artifact blob
  (raw body); `GET /artifact/{artifact_id}` — download it.
- Domain reads (POST, JSON body with a `Selection`): `/projects`,
  `/sweeps`, `/trials`, `/lineage`, `/executions`, `/trial-params`,
  `/value-catalog`, `/values`, `/artifacts`, `/provenance`.
- `POST /query` — raw read-only SQL escape hatch (`SELECT`/`WITH`/...,
  capped rows and runtime budget).
- `GET /api/health` — liveness.
- `/dashboard/...` — mounted read-only Dash UI. Browser login with the
  API key exchanges it for a signed session cookie; the key itself is
  never stored client-side.

The server address is set via:

```bash
export JERNERICS_TRACKING_SERVER="http://host:port"
```

Or in `pyproject.toml`:

```toml
[tool.jernerics]
tracking_server = "http://host:port"
```

When configured, each trial ships events live to the server over HTTP
(`StreamClient` tails the local JSONL file and POSTs batches to
`/ingest`). If the server is unreachable, events are still written
locally and replayed later.

## Event model (v3)

Sweeps own submissions (with scheduler jobs) and trials; trials own
executions; executions own values, progress, and artifacts.

- **Trial** — one optimizer trial in a sweep, identified by a UUID and
  a number. Retry lineage (`retry_of_trial_id`, `retry_root_trial_id`,
  `retry_index`) chains retries into families.
- **Execution** — one attempt to run a trial. Heartbeats and explicit
  progress (`current`/`total`/`unit`) attach to executions; monitoring
  labels (active/quiet/stale/ended) are derived on read from the last
  heartbeat, never stored.
- **Params** — `sampled` (drawn by the optimizer, recorded in the trial
  snapshot) or `manual` (logged by user code via `log_param`).
- **Values** — scalar floats or JSON observations keyed by name, with
  an optional integer step and a flat scalar `context`
  (`{"mode": "a"}`). JSON observations are bounded to 64 KiB encoded;
  anything bigger is an artifact, not a value.
- **Artifacts** — immutable blobs, two-phase: the declaration ships
  with the event log (filename, size, sha256, content type); the blob
  follows via `PUT /artifact/{id}`. Repeating a key creates versions
  v1..vN under that key; first received bytes win, later differing
  bytes are a 409 conflict. Stored stdout/stderr arrive the same way as
  `source="system"` artifacts keyed `stdout`/`stderr`.

## Local tracking data

Trial events are written as JSONL (one event per line) to the local
tracking directory:

```
~/.cache/jernerics/<project>/tracking/<sweep>/events/*.jsonl
```

The format is human-readable — inspect a file directly to debug a
trial. A `.cursor` sidecar records the last server-acknowledged byte
offset; replay resumes exactly there.

## Replay

`jernerics tracking replay` ships tracking data to the server, in one
of two modes:

```bash
jernerics tracking replay                       # local cache → server
jernerics tracking replay --study <sweep>
jernerics tracking replay --dry-run             # compare against server, send nothing
jernerics tracking replay --json

jernerics tracking replay -b <name>             # pull backend cache → server
jernerics tracking replay -b <name> --study <sweep>
```

Without `--backend`, the local cache (`cache_dir()/tracking`) is
replayed (`--study`, `--tracking-dir`, `--server`, `--dry-run`,
`--json` all apply). With `--backend NAME`, the backend's remote
tracking cache is pulled first, then shipped (`--study` applies; the
local-only options are rejected with an error rather than silently
ignored).

The post-hook pipeline also runs replay automatically after a sweep
completes, plus a journal reconciliation pass that snapshots every
optimizer trial as a terminal trial snapshot. Ingest is idempotent per
event id, so live shipping, replay, and reconciliation overlapping is
safe.

## Reading data back

- **Typed client** — `TrackingClient` / `ProjectHandle`
  (`jernerics.tracking`): typed records, opaque pagination, no SQL.
  Selections can be encoded as opaque tokens
  (`jernerics_schema.encode_selection`/`decode_selection`) to hand from
  the dashboard to a notebook or script.
- **CLI** — `jernerics tracking runs | summary | diff | trace | query`.
- **Raw SQL** — `POST /query` (also `TrackingClient.raw_query`) for
  anything the domain reads do not cover.

## Environment variables

| Variable | Purpose |
|----------|---------|
| `JERNERICS_TRACKING_SERVER` | Tracking HTTP server base URL (`http://host:port`) |
| `JERNERICS_API_KEY` | Optional bearer token; must match server and client |
