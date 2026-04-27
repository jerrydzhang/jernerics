# sweep-parallel

Integration test for the jernerics CLI against a real SLURM cluster. Exercises the parallel array job path with `max_parallel=2`.

## What this tests

- `build --backend hpc` — container build via SLURM
- `run --backend hpc` — parallel sweep submission (10 trials, 2 concurrent)
- `jobs --backend hpc` — job listing
- `logs --backend hpc` — log retrieval for array jobs
- `cancel --backend hpc` — job cancellation
- `clean --backend hpc` — artifact cleanup

## Prerequisites

1. **SSH access** to `jez21005@hpc2.storrs.hpc.uconn.edu` — verify with `ssh jez21005@hpc2.storrs.hpc.uconn.edu echo ok`
2. **jernerics installed** and on the `human-ownership` branch — `uv sync` from `packages/jernerics/`
3. **uv.lock present** — required for container build. Already in this directory.

## Integration test procedure

Run these commands from this directory (`examples/sweep-parallel/`).

### Step 1: Pre-flight

```bash
jernerics jobs --backend hpc
```

**Pass:** Completes without error (may print "No jobs found" or list existing jobs).
**If fail:** Check SSH access, then check that `pyproject.toml` has `[tool.jernerics.backends.hpc]` section.

### Step 2: Build container

```bash
jernerics build --backend hpc
```

**Pass:** Prints "Build job submitted: <id>".

Then wait for completion:
```bash
jernerics logs --backend hpc <id> --follow
```

Look for "Build completed at" in the output. Then verify:
```bash
ssh jez21005@hpc2.storrs.hpc.uconn.edu "ls ~/projects/jernerics-examples/sweep-parallel/container.sif"
```

**If fail:** Check that `container.def` and `uv.lock` exist locally. Check SLURM partition is valid.

### Step 3: Dry run

```bash
jernerics run --backend hpc dag.py config.py --dry-run
```

**Pass:** Prints "=== DRY RUN ===", "Backend: hpc", host info, and the full SLURM script. Does NOT submit a job.
**Verify the script contains:**
- `#SBATCH --array=1-10%2` (10 trials, max 2 concurrent)
- `apptainer exec` with `--bind` for `/work` and `/cache`
- `python -m jernerics.runner` invocation

### Step 4: Submit sweep

```bash
jernerics run --backend hpc dag.py config.py
```

**Pass:** Prints "[1/4] Syncing project..." through "[4/4] Submitting job...", then "Job submitted: <id>".
**Record the job ID for subsequent steps.**

### Step 5: Check jobs

```bash
jernerics jobs --backend hpc
```

**Pass:** Shows the submitted job in the table.

```bash
jernerics jobs --backend hpc --json
```

**Pass:** Valid JSON array with `job_id`, `name`, `status` keys.

### Step 6: Read logs

Wait a moment for the job to start, then:

```bash
jernerics logs --backend hpc <id> --array-index 1
```

**Pass:** Shows log output for trial 1. Should contain DAG task execution (generate_data → train → evaluate).

```bash
jernerics logs --backend hpc <id> --array-index 1 --stderr
```

**Pass:** Shows stderr (may be minimal).

### Step 7: Cancel (optional)

Submit another job and cancel it:
```bash
jernerics run --backend hpc dag.py config.py
jernerics cancel --backend hpc <new_id>
```

**Pass:** Prints "Cancelled job <id>".
**Note:** May fail with "Failed to cancel" if the job completed before cancellation — that's acceptable.

### Step 8: Clean (dry run)

```bash
jernerics clean --backend hpc --all
```

**Pass:** Shows "Would delete from..." and "Dry run. Use --force to actually delete." Does NOT delete anything.

### Step 9: Run local

```bash
jernerics run local dag.py config.py
```

**Note:** `config.py` has `n_trials = 10`. For faster local testing, you can create a temp config with `n_trials = 1`.

**Pass:** Runs all trials locally, prints "Best value:" at the end.

## Expected DAG data flow

```
generate_data(config) → {"seed": 42, "n_samples": 1000}
    ↓
train(generate_data, config) → {"loss": <float>, "lr": <float>, "dropout": <float>}
    ↓
evaluate(train, config) → {"loss": <float>, "accuracy": <float>}
```

The `objective` function in `config.py` extracts `results["evaluate"]["loss"]` for Optuna to minimize. The optimal parameters should be around `lr ≈ 0.001` and `dropout ≈ 1.0` (based on `fake_loss`).

## What NOT to do

- Do NOT run `clean --force` unless you want to rebuild the container
- Do NOT modify `pyproject.toml` — it has real HPC credentials
- Do NOT run `sync --backend hpc` — no tracking server is configured
- If a test fails, record the full error output before trying anything else
