# E2E Test Guide

Three backend+container combinations to verify:

| Backend | Container | Host | What it tests |
|---------|-----------|------|---------------|
| `hpc` | apptainer | `hpc2.storrs.hpc.uconn.edu` | Slurm array jobs + Apptainer build staging (`build_dir`) |
| `pueue-remote` | docker | `scimlab.engr.uconn.edu` | Pueue on remote + Docker build + run |
| `pueue-local` | none | local | Pueue on localhost + no container passthrough |

Run all commands from `examples/sweep-parallel/`.

## Prerequisites

```bash
# Verify SSH to both hosts
ssh jez21005@hpc2.storrs.hpc.uconn.edu echo ok
ssh jez21005@scimlab.engr.uconn.edu echo ok

# Verify local pueue daemon
pueue status

# Verify remote pueue daemon
ssh jez21005@scimlab.engr.uconn.edu pueue status

# Verify Docker on scimlab
ssh jez21005@scimlab.engr.uconn.edu docker ps
```

If any prerequisite fails, **stop and report the failure**.

---

## Test 1: HPC (Slurm + Apptainer with build_dir staging)

### 1a. Pre-flight

```bash
jernerics jobs --backend hpc
```

**Pass:** Completes without error (may show "No jobs found" or existing jobs).
**Fail:** Check SSH access and `[tool.jernerics.backends.hpc]` in `pyproject.toml`.

### 1b. Build container (staged on /dev/shm)

```bash
jernerics build --backend hpc
```

**Pass:** Prints "Build job submitted: <id>".

Wait for completion:
```bash
jernerics logs --backend hpc <id> --follow
```

**Verify the build used staged directory:**
After the build completes, check the build log output. It should contain:
- `mkdir -p /dev/shm/build/sweep-parallel`
- `cp .../container.def /dev/shm/build/sweep-parallel/`
- `cd /dev/shm/build/sweep-parallel`
- `cp container.sif ...` (copy back)
- `rm -rf /dev/shm/build/sweep-parallel`

It should **NOT** contain `APPTAINER_TMPDIR` or `cd ~/projects/...` before the build command.

Verify the container exists:
```bash
ssh jez21005@hpc2.storrs.hpc.uconn.edu "ls ~/projects/jernerics-examples/sweep-parallel/container.sif"
```

**Fail:** If build log shows `APPTAINER_TMPDIR=/dev/shm/...`, the old hack is still present. If it shows a plain `cd` to the project dir without staging, `build_dir` is not being passed through.

### 1c. Dry run

```bash
jernerics run --backend hpc dag.py config.py --dry-run
```

**Pass:** Prints "=== DRY RUN ===" with the full SLURM script.
**Verify the script contains:**
- `#SBATCH --array=1-10%2`
- `apptainer exec` with `--bind`
- `python -m jernerics.runner`

### 1d. Submit sweep

```bash
jernerics run --backend hpc dag.py config.py
```

**Pass:** Prints syncing steps, then "Job submitted: <id>".
Record the job ID.

### 1e. Check jobs and logs

```bash
jernerics jobs --backend hpc
```

**Pass:** Shows the job.

Wait for it to start, then:
```bash
jernerics logs --backend hpc <id> --array-index 1
```

**Pass:** Shows DAG task execution output (generate_data → train → evaluate).

---

## Test 2: Remote Pueue + Docker

### 2a. Build container (Docker image)

```bash
jernerics build --backend pueue-remote
```

**Pass:** Prints "Build job submitted: <id>".

Wait for the pueue task to complete:
```bash
ssh jez21005@scimlab.engr.uconn.edu pueue status
```

Once the build task is done, check logs:
```bash
ssh jez21005@scimlab.engr.uconn.edu pueue log <task_id>
```

**Pass:** Shows `docker build` output ending with "Successfully tagged container.sif".

Verify the image exists:
```bash
ssh jez21005@scimlab.engr.uconn.edu docker image inspect container.sif
```

**Fail:** If `docker build` fails, check that Dockerfile was synced to the remote:
```bash
ssh jez21005@scimlab.engr.uconn.edu "ls ~/projects/jernerics-examples/sweep-parallel/Dockerfile"
```

### 2b. Submit sweep

```bash
jernerics run --backend pueue-remote dag.py config.py
```

**Pass:** Prints "Sweep submitted: group <name>".

### 2c. Check status and logs

```bash
ssh jez21005@scimlab.engr.uconn.edu pueue status
```

Wait for tasks to complete, then:
```bash
jernerics logs --backend pueue-remote <task_id>
```

**Pass:** Shows trial output with DAG execution.

---

## Test 3: Local Pueue + No Container

### 3a. Dry run

```bash
jernerics run --backend pueue-local dag.py config.py --dry-run
```

**Pass:** Prints "=== DRY RUN ===" with pueue-local info.

### 3b. Submit sweep

```bash
jernerics run --backend pueue-local dag.py config.py
```

**Pass:** Prints "Sweep submitted: group <name>".

### 3c. Check status and logs

```bash
pueue status
```

Wait for tasks to complete, then:
```bash
jernerics logs --backend pueue-local <task_id>
```

**Pass:** Shows trial output with DAG execution and "Best value:" at the end.

### 3d. Clean up

```bash
pueue clean
```

---

## Cleanup

After all tests complete, clean up remote state:

```bash
# Clean HPC artifacts (dry run first)
jernerics clean --backend hpc
# If everything passed, force clean:
# jernerics clean --backend hpc --force --all

# Clean remote pueue state
ssh jez21005@scimlab.engr.uconn.edu pueue clean

# Clean local pueue state
pueue clean
```

## Reporting

For each test step, report:
1. **Step identifier** (e.g., "1b", "2a")
2. **PASS/FAIL**
3. **Full output** on failure (stdout + stderr)
4. **Hypothesis** if the cause is obvious from the error

Do not attempt to fix failures. Report them and stop.
