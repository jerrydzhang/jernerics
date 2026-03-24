# Jernerics

Opinionated utilities for ML projects. Provides DAG-based experiment execution, HPC cluster management via SLURM, and container-based reproducibility.

## Features

- **DAG-based experiment orchestration** - Define tasks with dependencies, execute in parallel
- **HPC integration** - Submit jobs to SLURM clusters, monitor progress, retrieve results
- **Container support** - Build and run experiments in Apptainer/Singularity containers
- **State management** - Resume interrupted runs, track experiment provenance
- **CLI tools** - Simple commands for common workflows

## Installation

```bash
pip install git+https://github.com/jerrydzhang/jernerics.git
```

Or with uv:

```bash
uv add git+https://github.com/jerrydzhang/jernerics.git
```

## Quick Start

### 1. Initialize a project

```bash
jernerics init my-project
cd my-project
```

This creates:
- `pyproject.toml` with jernerics configuration
- `container.def` for building containers
- `src/` directory structure

### 2. Define your DAG

Create `dag.py`:

```python
from jernerics.dag import DAG, task

dag = DAG()

@task
def load_data(config):
    # Load and preprocess data
    return {"data": [...]}

@task(depends_on=[load_data])
def train(load_data, config):
    # Train model
    return {"model": ...}

@task(depends_on=[train])
def evaluate(train, config):
    # Evaluate model
    return {"metrics": {...}}
```

### 3. Create a configuration file

Create `config.py`:

```python
configs = [
    {"lr": 0.001, "batch_size": 32},
    {"lr": 0.01, "batch_size": 64},
]

slurm = {
    "partition": "priority",
    "time": "2:00:00",
    "mem": "16G",
}

max_workers = 4  # Parallel task execution
```

### 4. Run experiments

**Locally:**
```bash
jernerics run local dag.py config.py
```

**On HPC (SLURM):**
```bash
jernerics container build    # Build container on HPC
jernerics run slurm dag.py config.py
```

## CLI Reference

### Initialization

```bash
jernerics init [project_dir] [--template python|cuda]
```

### Running Experiments

```bash
jernerics run local <dag_file> <config_file> [options]
  --results-dir, -r    Directory for results (default: results)
  --container, -c      Path to container file
  --gpu/--no-gpu       Enable GPU support (default: enabled)

jernerics run slurm <dag_file> <config_file> [options]
  --results-dir, -r    Directory for results
  --set, -S            Set SLURM option (key=value)
  --dry-run            Preview without submitting
```

### Container Management

```bash
jernerics container build [project_dir] [--force] [--dry-run]
```

### Job Management

```bash
jernerics jobs [--all] [--json]              # List jobs
jernerics logs <job_id> [--follow] [-i]      # View logs
jernerics cancel <job_id> [--all]            # Cancel jobs
jernerics results <job_id> [--local-dir]     # Download results
```

### Interactive Shell

```bash
jernerics shell [--gpu N] [--cpus N] [--mem 4G] [--time 1:00:00]
```

### Cleanup

```bash
jernerics clean [--results] [--logs] [--container] [--all] [--force]
```

## Configuration

Add to `pyproject.toml`:

```toml
[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"

[tool.jernerics.container]
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4

[tool.jernerics.shell]
partition = "priority"
cpus = 1
mem = "4G"
gpu = 0
```

Override HPC host via environment variable:
```bash
export JERNERICS_HPC_HOST="user@cluster.edu"
```

## DAG Tasks

Tasks are functions decorated with `@task`. Dependencies are injected automatically:

```python
from jernerics.dag import task

@task
def step_a(config):
    return {"result": 1}

@task(depends_on=[step_a])
def step_b(step_a, config):
    # step_a is the return value from step_a
    return step_a["result"] + 1

@task(depends_on=[step_a])
def step_c(step_a, config):
    # Independent of step_b, runs in parallel
    return step_a["result"] * 2
```

The `config` parameter receives the current configuration from `configs` list.

## Resume Interrupted Runs

Jernerics saves state to `.jernerics/runs/`. Resume a run:

```python
from jernerics.dag import DAG

dag = DAG("dag.py")
results = dag.resume(config, config_index=0)
```

## Requirements

- Python 3.12+
- HPC cluster with SLURM (for remote execution)
- Apptainer/Singularity (for containers)

## License

MIT
