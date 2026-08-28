# Jernerics

Experiment runner with multi-backend execution, hyperparameter sweeps, and tracking. Author a `trial(config, tracker)` function, sweep over configs with Optuna, and run locally or on Slurm/Pueue clusters — without code changes.

## Installation

```bash
uv add git+https://github.com/jerrydzhang/jernerics.git
```

Or with pip:

```bash
pip install git+https://github.com/jerrydzhang/jernerics.git
```

## Quick start

### 1. Initialize a project

```bash
jernerics init my-project
cd my-project
```

Creates `pyproject.toml`, `container.def`, `Dockerfile`, `src/`, and starter `trial.py` + `config.py`.

### 2. Define your trial

`trial.py`:

```python
def trial(config, tracker):
    data = load_data(config["seed"])
    model = train(data, lr=config["lr"])
    accuracy = evaluate(model)
    tracker.log_value("accuracy", accuracy)
    tracker.log_artifact("model", "model.pt")
    return {"loss": 1.0 - accuracy}
```

`config` holds the current hyperparameters (`base` merged with sampled `search_space`). Use `tracker.log_value` / `log_param` / `log_json` / `log_artifact` to record what matters. Return a dict; the `objective` lambda reads it.

### 3. Create a config

`config.py`:

```python
import optuna

base = {"seed": 42}

def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-5, 1e-1, log=True),
        "dropout": trial.suggest_float("dropout", 0.0, 1.0),
    }

n_trials = 50
objective = lambda results: results["loss"]
direction = "minimize"

backend_overrides = {
    "hpc": {"partition": "priority", "time": "2:00:00", "mem": "16G"},
}
```

For a single run without sweeps, omit `search_space` and set `n_trials = 1`.

### 4. Run

```bash
# Local test first
jernerics local trial.py config.py

# Then on a backend
jernerics run --backend hpc trial.py config.py
```

## Concepts

- **Trial** — A `trial(config, tracker)` function authored by the user. `config` is the merged hyperparameters; `tracker` records metrics/params/results/artifacts.
- **Sweep** — An Optuna-driven search over a `search_space` function. Each sample is one trial invocation.
- **Backend** — An execution target: local, Slurm, or Pueue. Configured in `pyproject.toml`.
- **Tracking** — An HTTP tracking server ingesting tagged events (JSONL over HTTP) and serving typed domain reads, a dashboard, and a raw SQL escape hatch. Live metrics stream during the run; a final replay guarantees delivery.
- **Artifact storage** — Artifact files served from the tracking server's disk over HTTP.
- **Retry** — Heartbeat-based failure detection with automatic resubmission for node deaths.

## CLI reference

| Command | Description |
|---------|-------------|
| `jernerics init <name>` | Scaffold a project |
| `jernerics local <trial> <config>` | Run a sweep locally |
| `jernerics run --backend <name> <trial> <config>` | Run on a backend |
| `jernerics interactive start --backend <name>` | Open or reconnect to a GPU shell (`--time`, `--gpus`, `--partition`, `--constraint`) |
| `jernerics interactive stop --backend <name>` | Tear down the interactive session |
| `jernerics interactive sync status --backend <name>` | Report code-sync session health (`--json`) |
| `jernerics interactive sync resolve <path>... --backend <name> --from local\|cluster` | Resolve conflicted paths with backups (`--dry-run`, `--yes`) |
| `jernerics job list --backend <name>` | List jobs (`--all`, `--json`) |
| `jernerics job cancel --backend <name> [id]` | Cancel jobs (`--all`) |
| `jernerics job logs --backend <name> <id>` | View logs (`--follow`, `--array-index`, `--stderr`) |
| `jernerics job wait --backend <name> <id>` | Block until a job finishes (`--timeout`, `--poll-interval`) |
| `jernerics backend build --backend <name>` | Build the container on the remote |
| `jernerics backend clean --backend <name>` | Delete remote artifacts (`--full`, `--force`) |
| `jernerics tracking replay [--backend <name>]` | Replay local cache (or pull from a backend) to the server (`--study`, `--dry-run`, `--json`) |
| `jernerics tracking runs` | List this project's trials with derived monitoring (`--json`) |
| `jernerics tracking summary <ref>` | One trial: lineage, params, values, artifacts, executions (`--json`) |
| `jernerics tracking diff <a> <b>` | Compare two trials: params, latest values, objective (`--json`) |
| `jernerics tracking trace <ref> <key>` | One value key's step series (`--json`) |
| `jernerics tracking query "<sql>"` | Raw read-only SQL escape hatch (`--json`) |


## Configuration

### pyproject.toml — backend profiles

```toml
[tool.jernerics.backends.hpc]
type = "slurm"
host = "user@cluster.edu"
remote_dir = "~/projects/{project_name}"
cache_dir = "/scratch/$USER/jernerics"

[tool.jernerics.backends.hpc.slurm]
partition = "priority"
time = "2:00:00"
mem = "16G"
cpus = 4

[tool.jernerics.backends.hpc.apptainer]
build_dir = "/dev/shm/build/{project_name}"

[tool.jernerics.backends.pueue-remote]
type = "pueue"
host = "user@workstation.edu"
remote_dir = "~/projects/{project_name}"
parallel = 2
container_type = "docker"

[tool.jernerics.backends.pueue-local]
type = "pueue"
parallel = 2
container_type = "none"
remote_dir = "."
```

### Config file — sweep variables

| Variable | Required | Description |
|----------|----------|-------------|
| `base` | Yes | Base config dict merged into every trial |
| `search_space(trial)` | No | Optuna sampling function |
| `n_trials` | Yes | Number of trials |
| `objective` | No | Lambda extracting metric from results |
| `direction` | No | `"minimize"` or `"maximize"` |
| `sampler` | No | Optuna sampler (default: `TPESampler`) |
| `backend_overrides` | No | Per-backend option overrides |

### `.jernericsignore` — project sync excludes

Every mechanism that transfers project source to a remote — deployment tar sync, interactive mutagen sync, and the one-shot fallback — applies one exclusion policy, composed in order: your `.gitignore` patterns, then `.jernericsignore` patterns, then a built-in list (`.git/`, `__pycache__/`, `*.pyc`, `*.sif`, `.cache/`, `results/`, `pools/`, `logs/`, `.venv/`, `venv/`, `*.egg-info/`, `.eggs/`, `build/`, `dist/`, `.mypy_cache/`, `.ruff_cache/`, `.hypothesis/`, `.pytest_cache/`, `.direnv/`).

Patterns use Git ignore syntax — root a pattern with a leading `/`, negate with `!`. Later sources win: `.jernericsignore` can re-include something `.gitignore` excluded, but the built-in list always wins and can never be negated. This is how you keep Git-tracked paths (e.g. `checkpoints/`) out of the remote copy. `.jernericsignore` itself synchronizes.

Mutagen locks its ignore set when a session is created: edits to `.gitignore` or `.jernericsignore` take effect the next time a session is intentionally created (a fresh allocation, or replacement of a stale session). A live or conflicted session is never restarted automatically — stop it and start again to apply the new policy.

### Environment variables

```bash
JERNERICS_TRACKING_SERVER   # Tracking HTTP server base URL (http://host:port)
JERNERICS_API_KEY           # Optional bearer token; must match on server and client
```

When `JERNERICS_API_KEY` is set in the server's environment, all requests (`/query`, `/ingest`, `/artifact`) must include an `Authorization: Bearer <key>` header. The same value is forwarded to remote backends automatically. If unset on the server, auth is disabled and all connections are accepted. Artifacts are stored on the server's disk (configured via `--artifacts-dir`); no external object storage is required.

## Interactive sync conflict resolution

`jernerics interactive sync status` lists conflicted paths. Conflicted files stop propagating in both directions (`two-way-safe` mode) until both sides agree. To resolve explicitly:

```bash
jernerics interactive sync resolve src/a.py src/b.py --backend hpc --from local
```

Every listed PATH must currently be conflicted and exist as a regular file on both sides; one `--from` side (local or cluster) wins the whole invocation. The command previews each transfer (direction, sizes, checksums, backup destination), asks once for confirmation (`--yes` for noninteractive use, `--dry-run` to preview only), then:

- backs up every losing copy locally under `$XDG_STATE_HOME/jernerics/sync-backups/<project>/<run>/` (default `~/.local/state/...`; cluster losers are downloaded there) and checksum-verifies all backups before overwriting anything;
- re-hashes both sides immediately before each overwrite (aborts on any mid-flight change) and lands each file via a verified temp file plus atomic rename, one path at a time;
- stops at the first failure — never rolls back — and reports completed, untouched, and unresolved paths;
- flushes the existing mutagen session and verifies every selected path left the conflict list.

Backups and a `manifest.json` per run are kept indefinitely; nothing is auto-deleted. The sync session itself is never restarted or replaced by resolution.


## Deployment

The tracking server is plaintext HTTP by design. For remote access over the public internet, terminate TLS in front of it. With [Tailscale Funnel](https://tailscale.com/kb/1223/funnel), `tailscaled` provisions a `*.ts.net` certificate and forwards plain HTTP to a localhost port — so the bearer API key travels encrypted over the Funnel's TLS and the server itself is never exposed directly.

With the NixOS module, bind to loopback and supply an API key:

```nix
services.jernerics.tracking = {
  enable = true;
  host = "127.0.0.1";                              # only localhost (tailscaled) reaches it
  httpPort = 8000;
  apiKeyFile = /run/secrets/jernerics-api-key;     # contains JERNERICS_API_KEY=...
};
```

Then point Funnel at it:

```bash
tailscale funnel --https=443 http://localhost:8000
```

On clients, set `JERNERICS_TRACKING_SERVER` to the Funnel URL (e.g. `https://your-host.ts.net`) and `JERNERICS_API_KEY` to the shared bearer.


## Tracking

### Server and dashboard

```bash
python -m jernerics_server --db /path/to/jernerics.sqlite --host 127.0.0.1 --http-port 8000 [--artifacts-dir /path/to/artifacts]
```

One process: SQLite store, artifacts on disk, the mounted dashboard.
With `JERNERICS_API_KEY` set, every endpoint requires
`Authorization: Bearer <key>` — and the dashboard's browser login
exchanges that key once for a signed session cookie, so the key itself
never stays in the browser. Tracking facts, artifacts, and scheduler state
are read-only in the dashboard: monitoring counts, sweep/trial/execution
pages, artifact and stored-log viewers, and cross-sweep analysis views. The
one write surface is sweep curation — authenticated dashboard sessions may
archive organizational history and mark scientifically invalid sweeps (a
reason is required); incomplete work stays visible in Current until it is
terminal, and invalid sweeps carry their reason and a data warning into
every analysis view. Series analysis stacks one panel per selected metric
with independent linear/log scales and custom ranges, overlays explicitly
on a shared axis when asked, compares dense sweeps through all-raw,
highlighted, or median+IQR display, and round-trips the whole view — scope,
keys, axes, filters, highlights — through the URL.

### Reading data back

```bash
jernerics tracking runs                  # trials + derived monitoring
jernerics tracking summary my-sweep:3    # lineage, params, values, artifacts
jernerics tracking diff my-sweep:3 my-sweep:7
jernerics tracking trace my-sweep:3 loss
jernerics tracking query "SELECT key, COUNT(*) FROM tracked_values GROUP BY key"
```

Programmatically, use the typed client — no SQL, no dataframe
dependency:

```python
from jernerics.tracking import TrackingClient, decode_selection, encode_selection

with TrackingClient("http://host:8000", api_key="...") as client:
    proj = client.project("my-project")
    trials = proj.trials(proj.selection())
    latest = proj.latest_values(proj.for_trials(trials[0].trial_id))
    print(proj.reduce("loss", fn="min"))

    # Selection handoff: the dashboard URL carries an opaque token;
    # decode it and read exactly what the page showed.
    token = encode_selection(proj.selection())
    assert decode_selection(token) == proj.selection()
```

`raw_query` is the one explicit SQL escape hatch for questions the
typed reads cannot answer.

### The tracker API

Inside a trial, `tracker` records what matters:

```python
def trial(config, tracker):
    tracker.log_param("seed", config["seed"])            # manual param
    for step in range(10):
        tracker.log_value("loss", loss(step), step=step, context={"phase": "train"})
    tracker.log_json("summary", {"accuracy": acc})        # JSON observation
    tracker.set_progress(step, 10, "epoch")               # explicit progress
    tracker.log_artifact("model", "model.pt")             # immutable artifact
    return {"loss": final_loss}
```

Values are scalars or JSON observations under a flat scalar `context`
(e.g. `{"phase": "train"}`) that becomes a queryable dimension. A JSON
observation is bounded to 64 KiB encoded — anything larger belongs in
an artifact, not a value.

### Artifacts, logs, and retries

- **Artifacts are immutable and two-phase**: the declaration (name,
  size, sha256) ships with the event log; the blob follows via HTTP.
  Repeating a key (`log_artifact("model", ...)` twice) creates
  versions v1..vN under that key — never an overwrite.
- **Stored stdout/stderr**: the runner captures each trial's child
  output and ships it as `system` artifacts keyed `stdout`/`stderr`,
  downloadable like any artifact.
- **Retry families**: a retried trial carries lineage
  (`retry_of`, `retry_root`, `retry_index`), so a family renders as
  generations in the CLI, client, and dashboard rather than loose
  duplicates.
- **Heartbeats**: running executions touch heartbeats; staleness
  (active / quiet / stale / ended) is derived on read from the last
  heartbeat and the execution's outcome — never stored, so it can
  never go stale itself.

## Container starters

`jernerics init` copies both `container.def` (Apptainer) and `Dockerfile` into the project. The format used depends on the backend's `container_type`:

| `container_type` | File | Runtime |
|------------------|------|---------|
| `apptainer` | `container.def` | Apptainer/Singularity |
| `docker` | `Dockerfile` | Docker |

**Rebuild when** `pyproject.toml` dependencies or the container definition file changes. Source code is bind-mounted at runtime.

## Trial authoring

A trial is a plain Python function. `config` holds the merged hyperparameters; `tracker` records what matters.

```python
def trial(config, tracker):
    model = train(lr=config["lr"], seed=config["seed"])
    accuracy = evaluate(model)

    tracker.log_value("accuracy", accuracy, step=config["config_index"])
    tracker.log_param("lr", config["lr"])
    tracker.log_value("summary", {"accuracy": accuracy, "lr": config["lr"]})
    tracker.log_artifact("model", "model.pt")

    return {"loss": 1.0 - accuracy}
```

The returned dict is passed to the config's `objective` lambda (e.g. `lambda results: results["loss"]`). For a long run, call `tracker.log_value(..., step=n)` repeatedly — values stream live to the server.

## Features

- **Tracking server** — A single HTTP process: ingests tagged events (`POST /ingest`), serves typed domain reads plus a dashboard whose only writes are sweep-curation metadata, raw SQL via `POST /query`, and artifact files (`GET /artifact/{id}`) from disk. Live metrics stream during the run; a final replay guarantees delivery.
- **Artifact storage** — Immutable two-phase artifacts with versions by repeated key, stored on the tracking server's disk over HTTP — no external object storage.
- **Retry system** — Heartbeat-based staleness detection for node deaths; retries carry lineage so families read as generations; configurable retries with persistent failure handling.
- **Post-hook pipeline** — After a sweep completes, reconciles the optuna journal with server state, replays tracking data, and uploads pending artifact blobs.
