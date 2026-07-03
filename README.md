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
    tracker.log_metric("accuracy", accuracy)
    tracker.log_artifact("model", "model.pt")
    return {"loss": 1.0 - accuracy}
```

`config` holds the current hyperparameters (`base` merged with sampled `search_space`). Use `tracker.log_metric` / `log_param` / `log_result` / `log_artifact` to record what matters. Return a dict; the `objective` lambda reads it.

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
- **Tracking** — An HTTP tracking server that ingests trial events (JSONL over HTTP) and serves them via a SQL `/query` endpoint. Live metrics stream during the run; a final replay guarantees delivery.
- **Artifact storage** — Artifact files served from the tracking server's disk over HTTP.
- **Retry** — Heartbeat-based failure detection with automatic resubmission for node deaths.

## CLI reference

| Command | Description |
|---------|-------------|
| `jernerics init <name>` | Scaffold a project |
| `jernerics local <trial> <config>` | Run a sweep locally |
| `jernerics run --backend <name> <trial> <config>` | Run on a backend |
| `jernerics build --backend <name>` | Build the container on the remote |
| `jernerics jobs --backend <name>` | List jobs (`--study`) |
| `jernerics cancel --backend <name> [id]` | Cancel jobs (`--all`) |
| `jernerics logs --backend <name> <id>` | View logs (`--follow`, `--array-index`, `--stderr`) |
| `jernerics clean --backend <name>` | Delete remote artifacts (`--full`, `--force`) |
| `jernerics sync --backend <name>` | Replay tracking data from remote to server (`--study`) |

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

### Environment variables

```bash
JERNERICS_TRACKING_SERVER   # Tracking HTTP server base URL (http://host:port)
JERNERICS_API_KEY           # Optional bearer token; must match on server and client
```

When `JERNERICS_API_KEY` is set in the server's environment, all requests (`/query`, `/ingest`, `/artifact`) must include an `Authorization: Bearer <key>` header. The same value is forwarded to remote backends automatically. If unset on the server, auth is disabled and all connections are accepted. Artifacts are stored on the server's disk (configured via `--artifacts-dir`); no external object storage is required.

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

    tracker.log_metric("accuracy", accuracy, step=config["config_index"])
    tracker.log_param("lr", config["lr"])
    tracker.log_result("summary", {"accuracy": accuracy, "lr": config["lr"]})
    tracker.log_artifact("model", "model.pt")

    return {"loss": 1.0 - accuracy}
```

The returned dict is passed to the config's `objective` lambda (e.g. `lambda results: results["loss"]`). For a long run, call `tracker.log_metric(..., step=n)` repeatedly — metrics stream live to the server.

## Features

- **Tracking server** — A single HTTP process: ingests trial events (`POST /ingest`, JSONL), serves them via SQL (`POST /query`), and serves artifact files (`GET /artifact/...`) from disk. Live metrics stream during the run; a final replay guarantees delivery.
- **Artifact storage** — Logged artifacts upload to the tracking server's disk over HTTP and are served back the same way — no external object storage.
- **Retry system** — Heartbeat-based staleness detection for node deaths; configurable retries with persistent failure handling.
- **Post-hook pipeline** — After a sweep completes, automatically replays tracking data and syncs artifacts to the server.
