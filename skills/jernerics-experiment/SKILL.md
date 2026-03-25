---
name: jernerics-experiment
description: |
  Use when running ML experiments, training, pipelines, or any DAG-based 
  workflow. Trigger on "run this", "train", "experiment", "pipeline", 
  "parallel configs", "workflow", or "execute". Handles both local and HPC 
  execution based on workload scope - the agent evaluates and chooses 
  appropriately. Also use when user explicitly mentions clusters, HPC, GPU, 
  or remote execution needs.
---

# Jernerics Experiment Runner

This skill enables autonomous execution of ML experiments using jernerics, a 
DAG-based experiment framework. Write experiments once, run them locally or 
on HPC without code changes.

## Vision

The agent's job is to:
1. Evaluate the scope of the workload
2. Write or verify the DAG and configuration
3. Execute locally or on HPC based on scope
4. Monitor progress and handle issues
5. Retrieve results automatically

## Quick Start

### Option 1: Initialize new project (recommended)

```bash
jernerics init my-project
cd my-project
```

This creates:
- `pyproject.toml` with jernerics configuration
- `container.def` for building Apptainer containers
- `src/` directory structure

Then create your DAG and config files.

### Option 2: Add to existing project

Add to your existing `pyproject.toml`:

```toml
[tool.jernerics.hpc]
host = "user@cluster.edu"
remote_dir = "~/projects/{project_name}"

[tool.jernerics.container]
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4
```

## Scope Evaluation

Decide between local and HPC execution based on:

| Factor | Local | HPC |
|--------|-------|-----|
| Duration | < 10 min | > 10 min or unknown |
| GPU needed | No | Yes |
| Parallel configs | 1-2 | 3+ |
| Memory | < 8GB | > 8GB or unknown |
| Data size | Small, local | Large, on cluster |

**Default**: When uncertain, start with a quick local test, then scale to HPC.

## Prerequisites Check

Before executing on HPC, verify:

1. **Jernerics config exists** in `pyproject.toml`:
   ```toml
   # Required: HPC connection settings
   [tool.jernerics.hpc]
   host = "user@cluster.edu"                           # SSH host (or set JERNERICS_HPC_HOST env var)
   remote_dir = "~/projects/{project_name}"            # Remote project directory
   cache_dir = "/scratch/$USER/jernerics"              # Optional: persistent cache for binds

   # Optional: Default SLURM settings for container builds
   [tool.jernerics.container]
   partition = "priority"                              # Default partition
   time = "1:00:00"                                    # Default time limit
   mem = "16G"                                         # Default memory
   cpus = 4                                            # Default CPU count

   # Optional: Safety limits
   [tool.jernerics.safety]
   max_concurrent_jobs = 10                            # Max parallel SLURM jobs

   # Optional: Interactive shell defaults
   [tool.jernerics.shell]
   partition = "priority-gpu"                          # Default shell partition
   cpus = 4                                            # Default shell CPUs
   mem = "32G"                                         # Default shell memory
   gpu = 1                                             # Default GPU count
   time = "2:00:00"                                    # Default shell time

   # Optional: Persistent directory binds (see "Advanced: Container Persistence")
   [tool.jernerics.binds]
   "/work/.julia_env" = "julia_env"                    # container_path = cache_subdir
   "/work/.julia_depot" = "julia_depot"
   ```

   **Minimal required config**: Only `[tool.jernerics.hpc]` with `host` is required. All other settings have sensible defaults.

2. **SSH access works**: Test with `ssh <host> 'echo ok'`

3. **Container exists** (or build it): Check if `container.sif` exists on remote

## DAG Structure

Use the DAG context manager to auto-register tasks. Dependencies are injected by function name.

```python
from jernerics.dag import DAG, task

with DAG() as dag:

    @task
    def load_data(config):
        data = ...  # load data
        return {"data": data, "n_samples": len(data)}

    @task(depends_on=[load_data])
    def preprocess(load_data, config):
        # load_data is the return value from the load_data task
        data = load_data["data"]
        return {"processed": processed_data}

    @task(depends_on=[preprocess])
    def train(preprocess, config):
        return {"model_path": "model.pt", "accuracy": 0.95}

    @task(depends_on=[train])
    def evaluate(train, config):
        return {"final_metrics": {...}}
```

**Key points**:
- Use `with DAG() as dag:` to auto-register decorated tasks
- Dependencies are injected as kwargs by function name: `depends_on=[load_data]` -> `def preprocess(load_data, ...)`
- `config` is always available, contains current hyperparameters
- Return dicts to pass data between tasks
- Tasks without dependencies run in parallel

**Serial execution**: For libraries incompatible with Python threading, use:
```python
dag.run(config, executor_type="serial")  # Runs tasks in main thread
```

## Configuration File

Create `config.py` alongside `dag.py`:

```python
from jernerics import merge_configs

# Shared base configuration
_base = {
    "seed": 42,
    "model": "gpt",
    "epochs": 10,
}

# Override specific values per experiment
configs = merge_configs(_base, [
    {"lr": 0.001, "batch_size": 32},
    {"lr": 0.01, "batch_size": 64},
    {"lr": 0.001, "batch_size": 32, "epochs": 20},  # Override epochs too
])

# SLURM settings (for HPC execution)
slurm = {
    "partition": "priority",       # Use "priority-gpu" for GPU queue
    "time": "2:00:00",
    "mem": "16G",
    "cpus": 4,
    "gres": "gpu:1",               # Optional: Request N GPUs (alternative to priority-gpu)
}

# Optional: Parallel task execution (default: CPU count, min 4 if undetectable)
# max_workers = 4

# Optional: Executor type - "thread" (default) or "serial"
# executor_type = "thread"
```

**Config format**:
- `configs`: List of dicts, each dict runs the full DAG once (required)
- `slurm`: SLURM settings for HPC (optional, empty dict if omitted)
- `max_workers`: Parallel task execution (optional, defaults to `min(cpu_count, 8)`)
- `executor_type`: `"thread"` for parallel or `"serial"` for sequential execution (optional, defaults to `"thread"`)

**GPU configuration**: Two approaches work:
1. Use `partition: "priority-gpu"` - routes to GPU queue (may have longer wait)
2. Use `gres: "gpu:N"` - requests N GPUs on any partition

## Execution Workflow

### 1. Local Test (Recommended First)

```bash
jernerics run local dag.py config.py
```

**Options**:
- `--results-dir, -r`: Directory for results (default: results)
- `--container, -c`: Path to container file (.sif or tarball) for testing containerized execution
- `--gpu/--no-gpu`: Enable GPU support via --nv flag (default: enabled)
- `--timeout, -t`: Timeout in seconds for each config run

Verify basic functionality before HPC submission.

### 2. Build Container (HPC Only)

```bash
jernerics container build --force
```

**Options**:
- `--force, -f`: Force rebuild even if up to date
- `--dry-run`: Preview actions without executing

Required once per project. Builds Apptainer container on HPC.

### 3. Dry Run (HPC Only)

```bash
jernerics run slurm dag.py config.py --dry-run
```

Review the SLURM script before submission.

### 4. Submit to HPC

```bash
jernerics run slurm dag.py config.py
```

**Options**:
- `--results-dir, -r`: Directory for results
- `--set, -S KEY=VALUE`: Override SLURM option (e.g., `--set time=4:00:00`)
- `--dry-run`: Preview without submitting

Outputs job ID. Record it for monitoring.

### 5. Monitor Job

```bash
jernerics jobs                    # List running jobs
jernerics jobs --all              # List all jobs including completed
jernerics logs <job_id> --array-index 1   # View logs for array job
```

**Note**: Array jobs (multiple configs) require `--array-index` for log viewing.

### 6. Retrieve Results

```bash
jernerics results <job_id>
```

Downloads results to `results/<job_id>/` locally. Do this automatically when job completes.

## Output Artifacts

Save results to the results directory, return summaries:

```python
import json
from pathlib import Path

@task(depends_on=[train])
def save_results(train, config):
    results_dir = Path(config.get("results_dir", "results"))
    results_dir.mkdir(exist_ok=True)
    
    # Save large artifacts
    model_path = results_dir / "model.pt"
    torch.save(train["model"], model_path)
    
    # Return lightweight summary
    return {
        "model_path": str(model_path),
        "accuracy": train["accuracy"],
        "config": config,
    }
```

### Provenance Tracking

Jernerics automatically tracks experiment provenance. Access it programmatically:

```python
from jernerics.dag import DAG, Provenance

# Provenance is saved to .jernerics/runs/<run_id>_provenance.json
# Contains: git SHA, jernerics version, config hash, container info, timestamps

# Read provenance from a previous run
provenance = Provenance.from_json(Path(".jernerics/runs/latest_provenance.json"))
print(f"Git SHA: {provenance.git_sha}")
print(f"Config: {provenance.config}")
```

This enables reproducibility and debugging of experiment conditions.

## Advanced: Container Persistence

Some libraries need persistent writable directories across runs (Julia environments, model checkpoints, cached datasets). Jernerics provides a bind mount system for this.

### The Problem

By default, containers are ephemeral - files written inside `/work` during one job aren't preserved for the next. This breaks libraries that install packages or cache data.

### Solution: cache_dir + binds

**1. Configure persistent cache location**:

```toml
[tool.jernerics.hpc]
cache_dir = "/scratch/$USER/jernerics"  # Fast, persistent storage
```

**2. Define bind mappings**:

```toml
[tool.jernerics.binds]
"/work/.julia_env" = "julia_env"      # container_path = cache_subdir
"/work/.julia_depot" = "julia_depot"
"/work/checkpoints" = "checkpoints"
```

This creates:
- On HPC: `/scratch/$USER/jernerics/<project>/julia_env` mounted to `/work/.julia_env`
- Locally: `~/.cache/jernerics/<project>/julia_env` (for testing)

**3. Use the paths API in your code**:

```python
from jernerics.paths import bind, work, is_hpc

# Check if running on HPC
if is_hpc():
    print("Running on HPC cluster")

# Get the work directory (/work on HPC, project dir locally)
results_dir = work() / "results"

# Get a bind-mounted directory
julia_env = bind("julia_env")  # Returns Path("/work/.julia_env") on HPC
os.environ["JULIA_PROJECT"] = str(julia_env)
```

**Alternative API via `paths` object**:

```python
from jernerics import paths

if paths.is_hpc:
    julia_env = bind("julia_env")  # bind() is still a function
    work_dir = paths.work
```

### When to Rebuild Containers

Understanding when containers need rebuilding saves time:

| Change Type | Rebuild Needed? | Why |
|-------------|-----------------|-----|
| Source code changes | **No** | Source is bind-mounted at runtime |
| Config changes | **No** | Config is passed at runtime |
| `pyproject.toml` dependencies | **Yes** | Dependencies are baked into container |
| `container.def` changes | **Yes** | Container definition changed |
| New binds added | **No** | Binds are mounted at runtime |

**Rule of thumb**: If it's in the container definition or lockfile, rebuild. If it's your code or config, no rebuild needed.

## Common Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| OOM | Memory too small | Increase `slurm["mem"]` |
| Timeout | Time too short | Increase `slurm["time"]` |
| Job not found | Wrong job ID | Check `jernerics jobs --all` |
| Log error | Array job without index | Add `--array-index N` |
| Missing dependency | Typo in `depends_on` | Use exact function name |
| Container missing | Not built | Run `jernerics container build` |
| DAG hangs | Threading incompatibility | Use `executor_type="serial"` |
| BindNotFound | Bind name not in config | Add to `[tool.jernerics.binds]` |

## Resume Failed Runs

Jernerics saves state to `.jernerics/runs/`. To resume:

```python
from jernerics.dag import DAG

with DAG("dag.py") as dag:
    results = dag.resume(config, config_index=0)
```

Useful for long runs that were interrupted.

## Job Management

```bash
jernerics cancel <job_id>         # Cancel specific job
jernerics cancel --all            # Cancel all your jobs
jernerics clean --all --force     # Clean remote artifacts
```

## Interactive Development

```bash
jernerics shell --gpu 1 --mem 16G --time 1:00:00
```

**Options**:
- `--gpu, -g N`: Number of GPUs (0 = no GPU)
- `--cpus, -c N`: Number of CPUs
- `--mem, -m SIZE`: Memory allocation (e.g., 4G)
- `--time, -t LIMIT`: Time limit (e.g., 1:00:00)
- `--partition, -p NAME`: Partition name
- `--no-container`: Enter shell without container

Get an interactive shell on HPC for debugging.

## CLI Reference

| Command | Description |
|---------|-------------|
| `jernerics init [dir]` | Create project scaffolding (`--template`, `--force`) |
| `jernerics run local <dag> <config>` | Run locally (`--results-dir`, `--container`, `--gpu/--no-gpu`, `--timeout`) |
| `jernerics run slurm <dag> <config>` | Submit to HPC (`--results-dir`, `--set`, `--dry-run`) |
| `jernerics container build` | Build on HPC (`--force`, `--dry-run`) |
| `jernerics jobs` | List jobs (`--all`, `--json`) |
| `jernerics logs <job_id>` | View logs (`--follow`, `--array-index`) |
| `jernerics results <job_id>` | Download results (`--local-dir`) |
| `jernerics shell` | Interactive shell (`--gpu`, `--cpus`, `--mem`, `--time`, `--partition`, `--no-container`) |
| `jernerics cancel <job_id>` | Cancel jobs (`--all`) |
| `jernerics clean` | Delete remote artifacts (`--results`, `--logs`, `--container`, `--all`, `--force`) |

## Full Examples

See complete working examples at:
https://github.com/jerrydzhang/jernerics/tree/main/examples/

- `container-basic/` - CPU-only workflow
- `container-gpu/` - GPU workflow with PyTorch

## Feedback

If you encounter friction points, confusing behavior, or missing documentation 
while using jernerics OR this skill, submit feedback via GitHub issues:

```bash
gh issue create --repo jerrydzhang/jernerics --title "<brief description>" --body "<details>"
```

Include:
- What you were trying to do
- What was confusing or didn't work
- Any workarounds you found
