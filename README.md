# Jernerics

A Python 3.12+ toolkit for building and evaluating ML models. Define experiments as DAGs of tasks, run them locally or on HPC clusters without code changes.

## Features

- **DAG-based experiments** - Define tasks with dependencies, automatic parallel execution
- **HPC integration** - Submit to SLURM clusters, monitor jobs, retrieve results
- **Container support** - Reproducible environments via Apptainer/Singularity
- **Provenance tracking** - Automatic git SHA, config hash, and timestamp logging
- **Resume capability** - Interrupted runs can be resumed from checkpoints

## Installation

```bash
uv add git+https://github.com/jerrydzhang/jernerics.git
```

Or with pip:

```bash
pip install git+https://github.com/jerrydzhang/jernerics.git
```

## Quick Start

### 1. Initialize a project

```bash
jernerics init my-project
cd my-project
```

Creates:
- `pyproject.toml` with `[tool.jernerics]` configuration
- `container.def` for Apptainer container
- `src/` directory structure

### 2. Define your DAG

Create `dag.py`:

```python
from jernerics.dag import DAG, task

with DAG() as dag:

    @task
    def load_data(config):
        data = ...  # load data
        return {"data": data, "n_samples": len(data)}

    @task(depends_on=[load_data])
    def preprocess(load_data, config):
        processed = ...  # preprocess load_data["data"]
        return {"processed": processed}

    @task(depends_on=[preprocess])
    def train(preprocess, config):
        model = ...  # train on preprocess["processed"]
        return {"model_path": "model.pt", "accuracy": 0.95}
```

**Key points:**
- Use `with DAG() as dag:` to auto-register decorated tasks
- Dependencies are injected by function name: `depends_on=[load_data]` → `def preprocess(load_data, ...)`
- `config` is always available, contains current hyperparameters
- Return dicts to pass data between tasks

### 3. Create a configuration file

Create `config.py`:

```python
import optuna

_base = {"seed": 42, "epochs": 10}

def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-5, 1e-1, log=True),
        "batch_size": trial.suggest_int("batch_size", 16, 128),
    }

n_trials = 50
objective_task = "train"
objective_metric = "loss"
direction = "minimize"

slurm = {
    "partition": "priority",
    "time": "2:00:00",
    "mem": "16G",
}

max_workers = 4  # Parallel task execution (default: CPU count)
```

For a single run without hyperparameter search, omit `search_space` and set `n_trials = 1` (default).

### 4. Run experiments

**Local test (recommended first):**
```bash
jernerics run local dag.py config.py
```

**On HPC:**
```bash
jernerics container build    # Build container on HPC (once per project)
jernerics run slurm dag.py config.py
```

### 5. Monitor and retrieve results

```bash
jernerics jobs                    # List running jobs
jernerics logs <job_id> -f        # Follow logs
jernerics results <job_id>        # Download results
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `jernerics init [dir]` | Create project scaffolding |
| `jernerics run local <dag> <config>` | Run locally |
| `jernerics run slurm <dag> <config>` | Submit to HPC |
| `jernerics container build` | Build container on HPC |
| `jernerics jobs` | List jobs (`--all` for completed) |
| `jernerics logs <job_id>` | View logs (`--follow`, `--array-index`) |
| `jernerics results <job_id>` | Download results |
| `jernerics shell` | Interactive shell on HPC |
| `jernerics cancel <job_id>` | Cancel jobs (`--all`) |
| `jernerics mlflow sync` | Sync mlflow runs from HPC to remote server |
| `jernerics clean` | Delete remote artifacts |

### Command Options

**`jernerics run local`**
- `--results-dir, -r` - Results directory (default: `results`)
- `--container, -c` - Path to container file
- `--gpu/--no-gpu` - Enable GPU support (default: enabled)
- `--timeout, -t` - Timeout per config in seconds

**`jernerics run slurm`**
- `--results-dir, -r` - Results directory
- `--set, -S KEY=VALUE` - Override SLURM option
- `--dry-run` - Preview without submitting

**`jernerics shell`**
- `--gpu, -g N` - Number of GPUs
- `--cpus, -c N` - Number of CPUs
- `--mem, -m SIZE` - Memory (e.g., `16G`)
- `--time, -t LIMIT` - Time limit (e.g., `2:00:00`)
- `--partition, -p NAME` - Partition name
- `--no-container` - Shell without container

**`jernerics container build`**
- `--force, -f` - Force rebuild
- `--dry-run` - Preview without executing

**`jernerics mlflow sync`**
Syncs mlflow runs from HPC scratch to the remote tracking server.
Requires `[tool.jernerics.mlflow]` and `cache_dir` to be configured.

## Configuration

### pyproject.toml

```toml
[tool.jernerics.hpc]
host = "user@cluster.edu"                    # SSH host (or set JERNERICS_HPC_HOST)
remote_dir = "~/projects/{project_name}"     # Remote project directory
cache_dir = "/scratch/$USER/jernerics"       # Optional: persistent cache

[tool.jernerics.container]
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4

[tool.jernerics.shell]
partition = "priority"
cpus = 4
mem = "32G"
gpu = 1

[tool.jernerics.binds]
"/work/.julia_env" = "julia_env"             # container_path = cache_subdir

[tool.jernerics.mlflow]
tracking_uri = "https://mlflow.example.com"    # Remote tracking server
username = "admin"
```

**Minimal config:** Only `[tool.jernerics.hpc]` with `host` is required.

### Environment Variables

```bash
export JERNERICS_HPC_HOST="user@cluster.edu"       # Override HPC host
export JERNERICS_MLFLOW_TRACKING_URI="https://..." # Override mlflow tracking URI
export JERNERICS_MLFLOW_USERNAME="admin"            # mlflow username
export JERNERICS_MLFLOW_PASSWORD="..."              # mlflow password
```

## DAG Tasks

### Dependencies

Tasks receive results from dependencies by parameter name:

```python
@task
def step_a(config):
    return {"result": 1}

@task(depends_on=[step_a])
def step_b(step_a, config):
    return step_a["result"] + 1

@task(depends_on=[step_a])
def step_c(step_a, config):
    return step_a["result"] * 2  # Runs in parallel with step_b
```

### Serial Execution

For libraries incompatible with threading:

```python
dag.run(config, executor_type="serial")
```

Or in config.py:

```python
executor_type = "serial"
```

## Output Artifacts

Use `work()` to get the correct results directory:

```python
from jernerics.paths import work

@task
def save_results(train, config):
    results_dir = work() / "results"
    results_dir.mkdir(exist_ok=True)
    
    model_path = results_dir / "model.pt"
    torch.save(train["model"], model_path)
    
    return {"model_path": str(model_path)}
```

## Provenance Tracking

Every run automatically tracks:

```python
from jernerics.dag import Provenance

provenance = Provenance.from_json(Path(".jernerics/runs/latest_provenance.json"))
print(f"Git SHA: {provenance.git_sha}")
print(f"Config: {provenance.config}")
```

Saved to `.jernerics/runs/{run_id}_provenance.json`.

## Resume Failed Runs

```python
from jernerics.dag import DAG

dag = DAG("dag.py")
results = dag.resume(config, config_index=0)
```

Skips completed tasks, re-runs failed ones.

## Container Persistence

For libraries that need persistent writable directories (Julia environments, checkpoints):

```toml
[tool.jernerics.hpc]
cache_dir = "/scratch/$USER/jernerics"

[tool.jernerics.binds]
"/work/.julia_env" = "julia_env"
"/work/checkpoints" = "checkpoints"
```

Use in code:

```python
from jernerics.paths import bind, work, is_hpc

if is_hpc():
    print("Running on HPC")

julia_env = bind("julia_env")  # /work/.julia_env in container
results_dir = work() / "results"
```

### When to Rebuild Containers

| Change Type | Rebuild? |
|-------------|----------|
| Source code | No (bind-mounted) |
| Config changes | No (passed at runtime) |
| `pyproject.toml` dependencies | Yes |
| `container.def` changes | Yes |
| New binds | No (mounted at runtime) |

## GPU Configuration

Two options:

1. **Use GPU partition:**
   ```python
   slurm = {"partition": "priority-gpu"}
   ```

2. **Request GPUs explicitly:**
   ```python
   slurm = {"gres": "gpu:1"}  # Request 1 GPU
   ```

## Common Issues

| Issue | Fix |
|-------|-----|
| OOM | Increase `slurm["mem"]` |
| Timeout | Increase `slurm["time"]` |
| Array job log error | Add `--array-index N` |
| DAG hangs | Use `executor_type="serial"` |
| BindNotFound | Add to `[tool.jernerics.binds]` |

## Requirements

- Python 3.12+
- HPC cluster with SLURM (for remote execution)
- Apptainer/Singularity (for containers)

## Examples

See complete examples in `examples/`:

- `sweep-basic/` - Optuna + MLflow sweep with synthetic loss surface
- `sweep-parallel/` - Parallel sweep with max_parallel constraint
- `no-objective-sweep/` - Sweep without optimization objective
- `gpu-smoke/` - GPU smoke test in container
- `resume-partial-failure/` - DAG resume after partial failure

## License

MIT
