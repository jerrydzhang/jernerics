---
name: jernerics-experiment
description: |
  Use when writing or running ML experiments, training pipelines, or DAG-based
  workflows with jernerics. Covers DAG authoring, config files, local/HPC
  execution, monitoring, and results retrieval. Trigger on "run experiment",
  "train", "pipeline", "dag", "config", "hyperparameter sweep", or when working
  with dag.py / config.py files in a jernerics project.
---

# Jernerics Experiment Runner

Write experiments as DAGs, run them locally or on HPC clusters without code changes.

## Before You Start

Ask the user:
1. Is this a GPU or CPU project?
2. Will they run on an HPC cluster? If so, is SSH access already configured?
3. How many parallel configurations do they expect?

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

See the `jernerics-hpc` skill for full configuration options.

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

### Context manager vs manual registration

`with DAG() as dag:` is preferred — tasks are auto-registered. The manual alternative:

```python
dag = DAG()
dag.add_task(my_task)
```

Use the context manager unless you need fine-grained control.

### Path handling

Always use `jernerics.paths` for file paths — they work correctly both locally and inside containers:

```python
from jernerics.paths import work, bind, is_hpc

@task
def save_results(config):
    # work() returns project root locally, /work on HPC
    results_dir = work() / "results"
    results_dir.mkdir(exist_ok=True)

    # bind() returns a persistent cache directory
    checkpoints = bind("checkpoints")  # must be configured in pyproject.toml

    # is_hpc() checks if running on cluster
    if is_hpc():
        ...
```

**Do not** use relative paths like `Path("results")` — they break inside containers.

### Config parameter naming conflict

If a task has a parameter named `config` that clashes with the injected config dict, jernerics warns and the DAG config overwrites it. Rename the parameter.

## Configuration File

Create `config.py` (or any name) alongside `dag.py`:

```python
from jernerics import merge_configs

# Shared base
_base = {"seed": 42, "epochs": 10}

# Each dict runs the full DAG once
configs = merge_configs(_base, [
    {"lr": 0.001, "batch_size": 32},
    {"lr": 0.01, "batch_size": 64},
])

# SLURM settings (HPC only)
slurm = {
    "partition": "priority",       # "priority-gpu" for GPU queue
    "time": "2:00:00",
    "mem": "16G",
    "gres": "gpu:1",               # Request GPUs
}

# Optional: parallel task execution (default: cpu_count, min 4)
# max_workers = 4

# Optional: "thread" (default) or "serial"
# executor_type = "thread"
```

**Required variables:**
- `configs`: list of dicts — each triggers a full DAG run

**Optional variables:**
- `slurm`: dict of SLURM options for HPC (default: `{}`)
- `max_workers`: int — thread pool size (default: `os.cpu_count() or 4`)
- `executor_type`: `"thread"` or `"serial"` (default: `"thread"`)

**GPU options:**
- Set `partition: "priority-gpu"` to route to GPU queue
- Set `gres: "gpu:N"` to request N GPUs

**Config overrides with `merge_configs`:**

```python
from jernerics import merge_configs

base = {"seed": 42, "model": "gpt", "epochs": 10}
configs = merge_configs(base, [
    {"lr": 0.001},                          # inherits everything from base
    {"lr": 0.01, "epochs": 20},             # overrides epochs too
])
# Result: [{"seed": 42, "model": "gpt", "epochs": 10, "lr": 0.001},
#          {"seed": 42, "model": "gpt", "epochs": 20, "lr": 0.01}]
```

## Execution

### 1. Local test

```bash
jernerics run local dag.py config.py
```

Options: `--results-dir`, `--container`, `--gpu/--no-gpu`, `--timeout`

### 2. Submit to HPC

```bash
jernerics run slurm dag.py config.py          # Submit
jernerics run slurm dag.py config.py --dry-run  # Preview SLURM script
jernerics run slurm dag.py config.py -S time=4:00:00  # Override SLURM option
```

Options: `--results-dir`, `--set KEY=VALUE`, `--dry-run`

**Prerequisites for HPC:** SSH access configured, container built (`jernerics container build`).

### 3. Monitor

```bash
jernerics jobs                     # Running jobs
jernerics jobs --all               # Include completed
jernerics logs <job_id> --follow   # Stream logs
jernerics logs <job_id> --array-index 1  # Array job logs
jernerics logs <job_id> --stderr   # Stderr instead of stdout
```

Array jobs (multiple configs) require `--array-index` to view specific task logs.

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
dag.add_task(my_task)
results = dag.resume(config, config_index=0)
```

- Completed tasks are skipped (uses saved output)
- Failed tasks are retried
- Can specify `run_id=` to resume a specific run, or omit to resume the latest

## Provenance

Every run automatically tracks: git SHA, jernerics version, Python version, platform, container path, SLURM job ID, timestamps. Saved to `.jernerics/runs/<run_id>_provenance.json`.

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
| `jernerics jobs` | List jobs (`--all`, `--json`) |
| `jernerics logs <job_id>` | View logs (`--follow`, `--array-index`, `--stderr`) |
| `jernerics results <job_id>` | Download results (`--local-dir`) |
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

## Examples

See `examples/` in the repo:
- `container-basic/` — CPU workflow
- `container-gpu/` — GPU workflow with PyTorch
