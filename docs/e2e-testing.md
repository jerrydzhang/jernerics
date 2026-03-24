# Jernerics E2E Test Procedure

Run this before every major change to confirm no regressions.

## Prerequisites

- [ ] Python 3.12+ with `uv` installed
- [ ] SSH access to an HPC cluster with SLURM + Apptainer
- [ ] `jernerics` installed (`uv sync` from repo root)
- [ ] Examples updated to latest jernerics commit (see "Pre-Test Setup" below)

---

## Pre-Test Setup

### 1. Update Examples to Latest Commit

```bash
# Get current commit
git rev-parse HEAD

# Update both examples (replace COMMIT with actual hash)
sed -i 's/rev = ".*"/rev = "COMMIT"/' examples/container-basic/pyproject.toml
sed -i 's/rev = ".*"/rev = ".*"/' examples/container-gpu/pyproject.toml

# Commit and push
git add -A && git commit -m "chore: update examples to latest jernerics"
git push
```

### 2. Verify HPC Config in Examples

Check that `examples/container-basic/pyproject.toml` and `examples/container-gpu/pyproject.toml` have correct:

```toml
[tool.jernerics.hpc]
host = "<netid>@<hpc-host>"  # Your HPC login
remote_dir = "~/projects/{project_name}"
```

> **Note:** The HPC hostname is public info, but your netid/username is personal. Consider using environment variables or `.env` for sensitive values in public repos.

---

## Part A: CPU-Only Project (container-basic)

### A1. Project Setup

```bash
cd examples/container-basic
uv sync
. .venv/bin/activate
```

---

### A2. Local DAG Execution (No Container)

```bash
jernerics run local dag.py config.py
```

**Verify:**
- [ ] Exit code 0
- [ ] Output contains "Running config 1/2" and "Running config 2/2"
- [ ] Output contains "DAG completed" twice
- [ ] `results/run1/` and `results/run2/` each contain `data.json`, `processed.json`, `summary.json`

**Clean up:**
```bash
rm -rf results/ .jernerics/runs/
```

---

### A3. Container Build (On HPC)

```bash
jernerics container build --force
```

**Verify:**
- [ ] Shows "[1/3] Syncing project..."
- [ ] Shows "[2/3] Uploading build script..."
- [ ] Shows "[3/3] Submitting build job..."
- [ ] Outputs job ID and `tail -f` command
- [ ] **CRITICAL:** Can tail the build log immediately:
  ```bash
  ssh <host> 'tail -f ~/projects/container-basic/build_<job_id>.out'
  ```
- [ ] Build completes and `container.sif` exists on remote (~118MB for CPU example)

**Common Issues:**
- If log file not found in `~/projects/container-basic/`, check `~/` (home dir) - indicates tilde expansion bug
- If build fails, check `.err` file for details

---

### A4. HPC Dry Run

```bash
jernerics run slurm dag.py config.py --dry-run
```

**Verify:**
- [ ] Output shows "=== DRY RUN ==="
- [ ] Shows correct host from `pyproject.toml`
- [ ] SLURM script contains `#SBATCH --array=1-2`
- [ ] Contains `apptainer exec` command
- [ ] References `container.sif`
- [ ] **No `~` in `#SBATCH --output` or `--error` directives** (should be relative or absolute paths)

---

### A5. HPC Submission

```bash
jernerics run slurm dag.py config.py
```

**Verify:**
- [ ] Shows "[1/3] Syncing project..."
- [ ] Shows "[2/3] Ensuring log directory..."
- [ ] Shows "[3/3] Submitting job..."
- [ ] Outputs job ID (record: ___________)
- [ ] `.jernerics/jobs/<job_id>.json` created locally

---

### A6. Monitor Job

```bash
# List running jobs
jernerics jobs

# View logs for array job (must specify array index)
jernerics logs <job_id> --array-index 1
jernerics logs <job_id> --array-index 2
```

**Verify:**
- [ ] Job appears in list
- [ ] Logs show "DAG completed" for each config
- [ ] Job shows COMPLETED in `jernerics jobs --all`

**Note:** `--follow` requires `--array-index` for array jobs.

---

### A7. Retrieve Results

```bash
jernerics results <job_id>
```

**Verify:**
- [ ] Creates `results/<job_id>/` locally
- [ ] Contains `run1/` and `run2/` with `data.json`, `processed.json`, `summary.json`

---

## Part B: GPU Project (container-gpu)

### B1. Project Setup

```bash
cd ../container-gpu
uv sync
. .venv/bin/activate
```

---

### B2. Container Build

```bash
jernerics container build --force
```

**Verify:**
- [ ] Build job submitted successfully
- [ ] Can tail build log: `ssh <host> 'tail -f ~/projects/container-gpu/build_<job_id>.out'`
- [ ] `container.sif` exists on remote (~2.7GB for GPU example with PyTorch)

---

### B3. HPC Dry Run

```bash
jernerics run slurm dag.py config.py --dry-run
```

**Verify:**
- [ ] SLURM script contains `#SBATCH --gres=gpu:1` or similar GPU directive
- [ ] Contains `apptainer exec --nv` (GPU passthrough)

---

### B4. HPC Submission (GPU Node)

```bash
jernerics run slurm dag.py config.py
```

**Verify:**
- [ ] Job submitted to GPU partition
- [ ] Record job ID: ___________

---

### B5. Monitor & Verify

```bash
jernerics logs <job_id> --array-index 1
```

**Verify:**
- [ ] Logs show DAG completed
- [ ] Job shows COMPLETED in `jernerics jobs --all`

**Note:** GPU detection depends on node assignment. The container works on CPU-only nodes too (PyTorch falls back).

---

### B6. Retrieve Results

```bash
jernerics results <job_id>
```

**Verify:**
- [ ] `results/<job_id>/gpu_test/` contains `gpu_info.json`, `compute.json`, `summary.json`

---

## Part C: Additional CLI Commands

### C1. Interactive Shell (Optional)

```bash
jernerics shell --help
```

**Verify:**
- [ ] Shows options for `--gpu`, `--cpus`, `--mem`, `--time`, `--partition`, `--no-container`

---

### C2. Clean Remote (Optional)

```bash
# Dry-run first
jernerics clean --all

# Verify it lists correct paths, then:
jernerics clean --all --force
```

**Verify:**
- [ ] Dry-run shows what would be deleted
- [ ] `--force` actually deletes on remote

---

### C3. Job Management

```bash
# List all jobs (including completed)
jernerics jobs --all

# Cancel specific job
jernerics cancel <job_id>

# Cancel all your jobs
jernerics cancel --all
```

---

## Summary Checklist

| Part | Step | Command | Status |
|------|------|---------|--------|
| Pre | Update examples to latest commit | `sed -i ...` | [ ] |
| Pre | Push changes | `git push` | [ ] |
| A | Local DAG | `jernerics run local` | [ ] |
| A | Container build | `jernerics container build` | [ ] |
| A | Verify build log location | `ssh ... tail -f ...` | [ ] |
| A | SLURM dry-run | `jernerics run slurm --dry-run` | [ ] |
| A | HPC submit | `jernerics run slurm` | [ ] |
| A | View logs | `jernerics logs --array-index` | [ ] |
| A | Get results | `jernerics results` | [ ] |
| B | GPU container build | `jernerics container build` | [ ] |
| B | GPU dry-run | `jernerics run slurm --dry-run` | [ ] |
| B | GPU submit | `jernerics run slurm` | [ ] |
| B | GPU results | `jernerics results` | [ ] |
| C | CLI commands | `jernerics jobs/clean/cancel` | [ ] |

---

## Known Issues to Watch For

### 1. Tilde (`~`) Not Expanded in SLURM Directives

**Symptom:** Build log files appear in `~/` instead of `~/projects/<name>/`

**Cause:** SLURM does NOT expand `~` or `$HOME` in `#SBATCH --output` directives

**Fix:** Code should use `expand_tilde()` to convert `~` to absolute path before generating SLURM scripts

**Test:** After `jernerics container build`, verify:
```bash
ssh <host> 'ls ~/projects/<name>/build_*.out'  # Should exist here
ssh <host> 'ls ~/build_*.out'  # Should NOT exist here (unless old files)
```

### 2. Array Jobs Require `--array-index` for `--follow`

**Symptom:** `jernerics logs <job_id> --follow` fails with "requires --array-index"

**Fix:** Use `jernerics logs <job_id> --array-index 1` for array jobs

### 3. `jernerics init --force` Resets HPC Config

**Symptom:** After running `jernerics init --force`, HPC host/remote_dir are reset to placeholders

**Fix:** Manually restore HPC config in `pyproject.toml` after running init

---

## Notes

- No local Docker/Apptainer required—all container testing happens on HPC
- `jernerics run slurm` auto-syncs code and checks for container.sif
- GPU test verifies CUDA availability and actual GPU computation
- Container sizes: ~118MB (CPU), ~2.7GB (GPU with PyTorch)
