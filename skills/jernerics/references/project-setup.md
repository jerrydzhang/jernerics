# Project Setup

## Adding jernerics to a project

```bash
uv add git+https://github.com/jerrydzhang/jernerics.git
```

Or initialize a new project:

```bash
jernerics init my-project
cd my-project
```

`jernerics init` creates:
- `pyproject.toml` with `[tool.jernerics]` config
- `container.def` (Apptainer definition)
- `Dockerfile` (Docker definition)
- `src/` directory

## pyproject.toml — backend profiles

Backends are named profiles under `[tool.jernerics.backends.*]`:

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

**Required fields per backend:**
- `type` — `"slurm"` or `"pueue"`
- `remote_dir` — project directory on the remote host

**Slurm-specific:**
- `host` — SSH target (`user@host`)
- `[tool.jernerics.backends.<name>.slurm]` — partition, time, mem, cpus, gres, max_parallel
- `[tool.jernerics.backends.<name>.apptainer]` — build_dir for staged builds

**Pueue-specific:**
- `host` — optional, omit for local pueue
- `parallel` — max concurrent tasks
- `container_type` — `"docker"`, `"apptainer"`, or `"none"`

## Container starters

`jernerics init --starter python` copies both `container.def` and `Dockerfile`.

Which format is used depends on `container_type` in the backend config:

| `container_type` | File | Runtime |
|------------------|------|---------|
| `apptainer` | `container.def` | Apptainer/Singularity |
| `docker` | `Dockerfile` | Docker |
| `none` | N/A | No container |

**Rebuild when** dependencies in `pyproject.toml` or the container
definition change. Source code is bind-mounted at runtime.

```bash
jernerics backend build -b hpc          # Build if stale
jernerics backend build -b hpc --force  # Force rebuild
jernerics backend build -b hpc --dry-run  # Preview
```

## Config file structure

A config file is a Python module loaded via `runpy.run_path()`. It
defines sweep parameters as module-level variables:

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
sampler = optuna.samplers.TPESampler(seed=42)

backend_overrides = {
    "hpc": {"partition": "priority", "time": "2:00:00", "mem": "16G"},
}
```

| Variable | Required | Description |
|----------|----------|-------------|
| `base` | Yes | Base config dict merged into every trial |
| `search_space(trial)` | No | Optuna sampling function |
| `n_trials` | Yes | Number of trials |
| `objective` | No | Lambda extracting metric from task results |
| `direction` | No | `"minimize"` or `"maximize"` |
| `sampler` | No | Optuna sampler instance |
| `backend_overrides` | No | Per-backend option overrides |

For a single run, omit `search_space` and set `n_trials = 1`.
