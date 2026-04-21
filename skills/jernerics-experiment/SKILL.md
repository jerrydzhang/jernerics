---
name: jernerics-experiment
description: |
  Use when writing or running ML experiments, training pipelines, or DAG-based
  workflows with jernerics. Covers DAG authoring, config files, hyperparameter
  sweeps with Optuna, MLflow logging, local/HPC execution, monitoring, and
  results retrieval. Trigger on "run experiment", "train", "pipeline", "dag",
  "config", "hyperparameter sweep", "optuna", "mlflow", or when working with
  dag.py / config.py files in a jernerics project.
---

# Jernerics Experiment Runner

Write experiments as DAGs, run them locally or on HPC clusters without code changes.

## Before You Start

Ask the user:
1. Is this a GPU or CPU project?
2. Will they run on an HPC cluster? If so, is SSH access already configured?
3. Do they want hyperparameter search (Optuna) or single runs?
4. Do they want MLflow tracking?

## Project Setup

### New project

```bash
jernerics init my-project
cd my-project
```

Creates `pyproject.toml` (with `[tool.jernerics]` config), `container.def`, and `src/`.
Edit `pyproject.toml` to add dependencies, then `uv sync`.

### Existing project

Ensure `[tool.jernerics]` config exists in `pyproject.toml`. At minimum:

```toml
[tool.jernerics.hpc]
host = "user@cluster.edu"
remote_dir = "~/projects/{project_name}"
```

See the jernerics-hpc skill for full configuration options.

## DAG Authoring

A DAG is a set of tasks with dependency relationships. Tasks are Python functions decorated with `@task`.

### Basic pattern

Use `with DAG() as dag:` to auto-register tasks:

```python
from jernerics.dag import DAG, task

with DAG() as dag:

    @task
    def load_data(config):
        return {"data": [1, 2, 3]}

    @task(depends_on=[load_data])
    def process(load_data, config):
        return {"result": [x * 2 for x in load_data["data"]]}

    @task(depends_on=[process])
    def save(process, config):
        return {"status": "done"}
```

### Dependency injection

Dependencies are injected by **function name matching**. If `depends_on=[load_data]`, then `load_data`'s return value is passed as the `load_data` parameter:

```python
@task(depends_on=[load_data])
def process(load_data, config):  # load_data receives the return value
    ...
```

**Rules:**
- `config` is always injected as the current hyperparameter dict
- Return dicts from tasks — they're passed to downstream tasks
- Tasks without dependencies run in parallel (thread pool)
- If a task fails, all downstream tasks are skipped with an `Exception` result
- Independent tasks still run even if another branch fails

### Serial execution

For libraries incompatible with Python threading:

```python
dag.run(config, executor_type="serial")
```

Or in `config.py`:

```python
executor_type = "serial"
```

### Path handling

Always use `jernerics.paths` for file paths — they work correctly both locally and inside containers:

```python
from jernerics.paths import work, bind, is_hpc

@task
def save_results(config):
    results_dir = work() / "results"
    results_dir.mkdir(exist_ok=True)

    if is_hpc():
        ...
```

**Do not** use relative paths like `Path("results")` — they break inside containers.

## Configuration File

Create `config.py` (or any name) alongside `dag.py`.

### Sweep with Optuna

```python
import optuna

_base = {"seed": 42, "epochs": 10}

def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-5, 1e-1, log=True),
        "batch_size": trial.suggest_int("batch_size", 16, 128),
        "dropout": trial.suggest_float("dropout", 0.0, 1.0),
    }

n_trials = 50
sampler = None  # None = default TPESampler (recommended for parallel HPC runs)
objective_task = "evaluate"    # task name whose result contains the metric
objective_metric = "loss"      # key in that task's return dict
direction = "minimize"

slurm = {
    "partition": "priority",
    "time": "2:00:00",
    "mem": "16G",
    "gres": "gpu:1",
    "max_parallel": 4,          # limit concurrent array tasks
}

max_workers = 2
executor_type = "thread"
```

### Single run (no search)

Omit `search_space` and `objective_task`/`objective_metric`. Set `n_trials = 1` (default). Single runs still get full MLflow tracking (all params logged as `base.*`):

```python
_base = {"seed": 42, "lr": 0.001, "batch_size": 32}

n_trials = 1

slurm = {
    "partition": "priority",
    "time": "1:00:00",
    "mem": "16G",
}
```

### Sweep without optimization

Run multiple configs through the DAG without Optuna optimization (e.g., profiling architectures):

```python
_base = {"input_dim": 784}

def search_space(trial):
    return {
        "hidden_size": trial.suggest_categorical("hidden_size", [64, 128, 256]),
    }

n_trials = 8
objective_task = None       # no optimization — just run all configs
objective_metric = None
direction = "minimize"      # doesn't matter when objective is None
```

### Config variables reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `_base` | Yes | `{}` | Base config dict merged into every trial |
| `search_space` | No | `None` | Function taking `optuna.Trial`, returns param dict |
| `n_trials` | No | `1` | Number of trials to run |
| `sampler` | No | `None` | Optuna sampler instance (`None` = default TPESampler) |
| `objective_task` | No | `None` | Task name for optimization metric |
| `objective_metric` | No | `None` | Key in that task's return value |
| `direction` | No | `"minimize"` | `"minimize"` or `"maximize"` |
| `slurm` | No | `{}` | SLURM options (partition, time, mem, gres, max_parallel) |
| `max_workers` | No | `None` | Thread pool size for DAG tasks |
| `executor_type` | No | `"thread"` | `"thread"` or `"serial"` |

### GPU options

- Set `partition: "priority-gpu"` to route to GPU queue
- Set `gres: "gpu:N"` to request N GPUs

### max_parallel for SLURM

In `slurm` dict, `max_parallel` controls how many array tasks run simultaneously:

```python
slurm = {"max_parallel": 4, ...}  # At most 4 trials run in parallel
```

Defaults to `max_concurrent_jobs` from `[tool.jernerics.safety]` (default: 10).

## MLflow Logging

### Setup

Add to `pyproject.toml`:

```toml
[tool.jernerics.mlflow]
tracking_uri = "https://mlflow.example.com"
username = "admin"
```

Or use environment variables:

```bash
export JERNERICS_MLFLOW_TRACKING_URI="https://mlflow.example.com"
export JERNERICS_MLFLOW_USERNAME="admin"
export JERNERICS_MLFLOW_PASSWORD="..."
```

### What gets auto-logged

Every trial (including single runs with `n_trials=1`) automatically logs to MLflow:

- **Params:** The full merged config (`_base` + `search_space`) is logged with namespacing:
  - `base.*` — values from `_base` (constant across trials)
  - `swept.*` — values from `search_space` (vary per trial)
  - Nested dicts are flattened with dots: `base.model.lr = 0.01`
- **Metrics:** The `objective_metric` from the `objective_task` return value
- Any additional `mlflow.log_metric()` / `mlflow.log_params()` calls in task bodies

**Overlap check:** Defining the same key in both `_base` and `search_space` raises a `ValueError`. Remove it from `_base`.

### How it works

When `[tool.jernerics.mlflow]` is configured:

- **Locally:** Each trial connects directly to the tracking URI
- **On HPC with `cache_dir`:** Each trial logs to a local file store at `/scratch/<project>/mlruns/`, then auto-syncs to the remote tracking server via `mlflow-export-import`
- **Manual sync:** Run `jernerics mlflow sync` to batch-sync any runs that weren't auto-synced

### Logging metrics in tasks

Use `mlflow.log_metric` with `jernerics.active_run_id`:

```python
import mlflow
from jernerics import active_run_id

@task
def evaluate(train, config):
    loss = compute_loss(train["predictions"], train["labels"])
    mlflow.log_metric("accuracy", 1.0 - loss, run_id=active_run_id)
    return {"loss": loss}
```

**Important:** Always pass `run_id=active_run_id` — the run is started by the sweep runner, not inside the task.

### Syncing runs

```bash
jernerics mlflow sync    # Syncs all unsynced runs from HPC scratch to remote server
```

Requires `[tool.jernerics.mlflow]` with `tracking_uri` and `[tool.jernerics.hpc]` with `cache_dir`.

## Execution

### 1. Local test

```bash
jernerics run local dag.py config.py
```

Options: `--results-dir`, `--container`, `--gpu/--no-gpu`, `--timeout`

For sweeps, Optuna study is created locally in `.jernerics/optuna/`. Best trial is printed at the end.

### 2. Submit to HPC

```bash
jernerics run slurm dag.py config.py          # Submit
jernerics run slurm dag.py config.py --dry-run  # Preview SLURM script
jernerics run slurm dag.py config.py -S time=4:00:00  # Override SLURM option
```

Options: `--results-dir`, `--set KEY=VALUE`, `--dry-run`

**Prerequisites for HPC:** SSH access configured, container built (`jernerics container build`).

Each trial becomes a SLURM array task. Optuna study uses a shared SQLite database on scratch.

### 3. Monitor

```bash
jernerics jobs                     # Running jobs
jernerics jobs --all               # Include completed
jernerics logs <job_id> --follow   # Stream logs
jernerics logs <job_id> --array-index 1  # Array job logs (required for sweeps)
jernerics logs <job_id> --stderr   # Stderr instead of stdout
```

Array jobs (multiple trials) require `--array-index` to view specific task logs.

### 4. Retrieve results

```bash
jernerics results <job_id>                    # Download to results/<job_id>/
jernerics results <job_id> --local-dir path   # Custom directory
```

### 5. Cancel

```bash
jernerics cancel <job_id>
jernerics cancel --all
```

## State and Resume

Jernerics auto-saves execution state to `.jernerics/runs/`. To resume a failed/interrupted run:

```python
from jernerics.dag import DAG

dag = DAG("dag.py")
results = dag.resume(config, config_index=0)
```

- Completed tasks are skipped (uses saved output)
- Failed tasks are retried

## Provenance

Every run automatically tracks: git SHA, config, timestamps. Saved to `.jernerics/runs/<run_id>_provenance.json`.

```python
from jernerics.dag import Provenance
p = Provenance.from_json(Path(".jernerics/runs/<run_id>_provenance.json"))
```

## Interactive Shell

```bash
jernerics shell                    # Defaults from pyproject.toml
jernerics shell --gpu 1 --mem 16G  # Override options
jernerics shell --no-container     # Raw shell, no container
```

Options: `--gpu`, `--cpus`, `--mem`, `--time`, `--partition`, `--no-container`

## CLI Reference

| Command | Description |
|---------|-------------|
| `jernerics init [dir]` | Create project (`--template`, `--force`) |
| `jernerics run local <dag> <config>` | Run locally (`--results-dir`, `--container`, `--gpu/--no-gpu`, `--timeout`) |
| `jernerics run slurm <dag> <config>` | Submit to HPC (`--results-dir`, `--set`, `--dry-run`) |
| `jernerics container build` | Build container on HPC (`--force`, `--dry-run`) |
| `jernerics mlflow sync` | Sync mlflow runs from HPC to remote server |
| `jernerics jobs` | List jobs (`--all`, `--json`) |
| `jernerics logs <job_id>` | View logs (`--follow`, `--array-index`, `--stderr`) |
| `jernerics results <job_id>` | Download results (`--local-dir`, `--clean-logs`) |
| `jernerics shell` | Interactive HPC shell (`--gpu`, `--cpus`, `--mem`, `--time`, `--partition`, `--no-container`) |
| `jernerics cancel <job_id>` | Cancel jobs (`--all`) |
| `jernerics clean` | Delete remote artifacts (`--results`, `--logs`, `--container`, `--all`, `--force`) |

## Common Issues

| Issue | Fix |
|-------|-----|
| OOM | Increase `slurm["mem"]` |
| Timeout | Increase `slurm["time"]` |
| DAG hangs | Use `executor_type="serial"` |
| Missing dependency | Check `depends_on` uses exact function reference |
| Array log error | Add `--array-index N` |
| Container missing on HPC | Run `jernerics container build` |
| mlflow sync fails | Check `cache_dir` and `tracking_uri` are configured |

## Examples

The jernerics repo (https://github.com/jerrydzhang/jernerics) includes examples:
- `sweep-basic/` — Optuna + MLflow sweep with synthetic loss surface
- `sweep-parallel/` — Parallel sweep with max_parallel constraint
- `no-objective-sweep/` — Sweep without optimization objective
- `gpu-smoke/` — GPU smoke test in container
- `resume-partial-failure/` — DAG resume after partial failure
