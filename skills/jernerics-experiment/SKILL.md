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

## Scope Evaluation

Decide between local and HPC execution based on:

| Factor | Local | HPC |
|--------|-------|-----|
| Duration | < 10 min | > 10 min or unknown |
| GPU needed | No | Yes (use "priority-gpu" partition) |
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
- Dependencies are injected as kwargs by function name: `depends_on=[load_data]` → `def preprocess(load_data, ...)`
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
    "partition": "priority",  # or "priority-gpu" for GPU jobs
    "time": "2:00:00",
    "mem": "16G",
    "cpus": 4,
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

## Execution Workflow

### 1. Local Test (Recommended First)

```bash
jernerics run local dag.py config.py
```

Verify basic functionality before HPC submission.

### 2. Build Container (HPC Only)

```bash
jernerics container build --force
```

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

Get an interactive shell on HPC for debugging.

## Full Examples

See complete working examples at:
https://github.com/jerrydzhang/jernerics/tree/main/examples/

- `container-basic/` - CPU-only workflow
- `container-gpu/` - GPU workflow with PyTorch

## Feedback

If you encounter friction points, confusing behavior, or missing features while 
using jernerics, submit feedback via GitHub issues:

```bash
gh issue create --repo jerrydzhang/jernerics --label friction --title "<brief description>" --body "<details>"
```

Include:
- What you were trying to do
- What was confusing or didn't work
- Any workarounds you found
