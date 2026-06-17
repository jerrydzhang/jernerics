# Jernerics

DAG-based experiment runner with multi-backend execution, hyperparameter sweeps, and tracking. Define tasks with dependencies, sweep over configs with Optuna, and run locally or on Slurm/Pueue clusters — without code changes.

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

Creates `pyproject.toml`, `container.def`, `Dockerfile`, and `src/`.

### 2. Define your DAG

`dag.py`:

```python
from jernerics.dag import DAG, task

with DAG() as dag:

    @task
    def load_data(config):
        data = ...
        return {"data": data, "n_samples": len(data)}

    @task(depends_on=[load_data])
    def train(load_data, config):
        model = ...
        return {"loss": 0.05, "model_path": "model.pt"}

    @task(depends_on=[train])
    def evaluate(train, config):
        accuracy = 1.0 - train["loss"]
        return {"accuracy": accuracy}
```

Dependencies are injected by parameter name. `config` is always available and contains the current hyperparameters. Return dicts to pass data between tasks.

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
objective = lambda results: results["evaluate"]["loss"]
direction = "minimize"

backend_overrides = {
    "hpc": {"partition": "priority", "time": "2:00:00", "mem": "16G"},
}
```

For a single run without sweeps, omit `search_space` and set `n_trials = 1`.

### 4. Run

```bash
# Local test first
jernerics local dag.py config.py

# Then on a backend
jernerics run --backend hpc dag.py config.py
```

## Concepts

- **DAG** — A directed acyclic graph of tasks. Tasks without dependencies run in parallel.
- **Task** — A function decorated with `@task`. Receives results from dependencies plus `config` and optional `tracker`.
- **Sweep** — An Optuna-driven search over a `search_space` function. Each sample is a **trial**.
- **Trial** — One execution of the full DAG with a specific set of hyperparameters.
- **Backend** — An execution target: local, Slurm, or Pueue. Configured in `pyproject.toml`.
- **Tracking** — gRPC-based tracking server that streams trial metrics and artifacts.
- **Artifact storage** — S3-compatible object storage (MinIO) for trial artifacts.
- **Retry** — Heartbeat-based failure detection with automatic resubmission for node deaths.

## CLI reference

| Command | Description |
|---------|-------------|
| `jernerics init [dir]` | Create project scaffolding (`--starter`, `--force`) |
| `jernerics local <dag> <config>` | Run in-process (no backend) |
| `jernerics run --backend <name> <dag> <config>` | Submit sweep to a backend (`--set`, `--dry-run`) |
| `jernerics build --backend <name>` | Build container on remote (`--force`, `--dry-run`) |
| `jernerics jobs --backend <name>` | List jobs (`--all`, `--json`) |
| `jernerics cancel --backend <name> [id]` | Cancel jobs (`--all`) |
| `jernerics logs --backend <name> <id>` | View logs (`--follow`, `--array-index`, `--stderr`) |
| `jernerics clean --backend <name>` | Delete remote artifacts (`--full`, `--force`) |
| `jernerics sync --backend <name>` | Replay tracking data from remote to server (`--study`) |

## Tracking HTTP Observability

Query sweep and trial data via HTTP commands. These use the tracking **HTTP server**, not the gRPC `tracking_server` address.

**Server URL resolution** (first wins):
1. `--server <url>` flag
2. `JERNERICS_TRACKING_HTTP_SERVER` environment variable
3. `[tool.jernerics].tracking_http_server` in `pyproject.toml`

| Command | Description |
|---------|-------------|
| `jernerics sweeps` | List sweeps |
| `jernerics trials <sweep_id>` | List trials for a sweep |
| `jernerics compare-sweeps <sweep_id...>` | Compare sweeps side-by-side |
| `jernerics metric-history <sweep_id> <metric>` | Plot metric over trials |
| `jernerics artifacts <sweep_id> [trial_id]` | List artifact paths |
| `jernerics results <sweep_id> [trial_id]` | Show trial results |
| `jernerics params <sweep_id> [trial_id]` | Show trial hyperparameters |
| `jernerics tracking-health` | Check server connectivity |

Add `--json` to any command for machine-readable output.

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
JERNERICS_TRACKING_SERVER   # gRPC tracking server (host:port)
JERNERICS_API_KEY           # Optional API key for gRPC auth (must match on server and client)
AWS_ENDPOINT_URL            # S3-compatible endpoint for artifact storage
AWS_ACCESS_KEY_ID           # S3 credentials
AWS_SECRET_ACCESS_KEY
JERNERICS_ARTIFACT_BUCKET   # Bucket name (default: "jernerics")
```

When `JERNERICS_API_KEY` is set in the server's environment, all gRPC calls must include a matching `x-api-key` header. Set the same value on the client side (it gets forwarded to remote backends automatically). If unset on the server, auth is disabled and all connections are accepted.

## Container starters

`jernerics init` copies both `container.def` (Apptainer) and `Dockerfile` into the project. The format used depends on the backend's `container_type`:

| `container_type` | File | Runtime |
|------------------|------|---------|
| `apptainer` | `container.def` | Apptainer/Singularity |
| `docker` | `Dockerfile` | Docker |

**Rebuild when** `pyproject.toml` dependencies or the container definition file changes. Source code is bind-mounted at runtime.

## DAG authoring

### Dependency injection

```python
@task
def step_a(config):
    return {"result": 1}

@task(depends_on=[step_a])
def step_b(step_a, config):      # step_a["result"] available
    return step_a["result"] + 1

@task(depends_on=[step_a])
def step_c(step_a, config):      # runs in parallel with step_b
    return step_a["result"] * 2
```

### Tracking

Inject the `Tracker` to log metrics and artifacts:

```python
from jernerics.tracking.tracker import Tracker

@task
def evaluate(train, config, tracker: Tracker):
    tracker.log_metrics({"accuracy": 0.95})
    tracker.log_artifact("summary.txt", "/path/to/summary.txt")
    return {"accuracy": 0.95}
```

### Serial execution

For debugging with pdb:

```python
from jernerics.dag.executor import SyncRunner
# In config.py:
runner = SyncRunner()
```

## Features

- **Tracking server** — gRPC stream of trial events (params, metrics, artifacts) to a central DuckDB-backed server.
- **Artifact storage** — Automatic upload of logged artifacts to S3-compatible storage via manifest-based batching.
- **Retry system** — Heartbeat-based staleness detection for node deaths; configurable retries with persistent failure handling.
- **Post-hook pipeline** — After a sweep completes, automatically replays tracking data and syncs to the server.

## Example

See [`example/`](example/) for a complete end-to-end setup with multiple backends, GPU detection, and retry configurations.
