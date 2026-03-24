# Jernerics E2E Test Procedure

## Prerequisites

- [ ] Python 3.12+ with `uv` installed
- [ ] SSH access to an HPC cluster with SLURM + Apptainer
- [ ] `jernerics` installed (`uv sync` from repo root)

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
- [ ] `.jernerics/runs/latest_0.json` and `latest_1.json` exist
- [ ] `results/run1/` and `results/run2/` each contain `data.json`, `processed.json`, `summary.json`

**Clean up:**
```bash
rm -rf results/ .jernerics/runs/
```

---

### A3. Container Build (On HPC)

```bash
jernerics container build .
```

**Verify:**
- [ ] Exit code 0
- [ ] `.jernerics/container.tar.gz` exists

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
# List jobs
jernerics jobs

# View logs
jernerics logs <job_id>

# Follow specific array task
jernerics logs <job_id> --follow --array-index 1
```

**Verify:**
- [ ] Job appears in list
- [ ] Logs show "DAG completed" for each config
- [ ] Job eventually shows COMPLETED in `jernerics jobs --all`

---

### A7. Retrieve Results

```bash
jernerics results <job_id>
```

**Verify:**
- [ ] Creates `results/<job_id>/` locally
- [ ] Contains `run1/` and `run2/` with all expected files

---

## Part B: GPU Project (container-gpu)

> **Note:** This example may need updates to match current jernerics. Check `pyproject.toml` jernerics rev and `[tool.jernerics]` config.

### B1. Project Setup

```bash
cd ../container-gpu
uv sync
. .venv/bin/activate
```

**If missing `[tool.jernerics]` config, add to pyproject.toml:**
```toml
[tool.jernerics.hpc]
host = "<your-hpc-host>"
remote_dir = "~/experiments/{project_name}"

[tool.jernerics.container]
partition = "priority-gpu"
time = "1:00:00"
mem = "16G"
cpus = 4

[tool.jernerics.shell]
partition = "priority-gpu"
cpus = 1
mem = "4G"
gpu = 1
```

---

### B2. Container Build

```bash
jernerics container build .
```

**Verify:**
- [ ] `.jernerics/container.tar.gz` exists (larger due to PyTorch CUDA)

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

### B5. Monitor & Verify GPU Usage

```bash
jernerics logs <job_id> --follow
```

**Verify:**
- [ ] Logs show `cuda_available: true`
- [ ] Logs show `device: cuda`
- [ ] Job completes successfully

---

### B6. Retrieve Results

```bash
jernerics results <job_id>
```

**Verify:**
- [ ] `results/<job_id>/gpu_test/gpu_info.json` shows:
  - `cuda_available: true`
  - `cuda_version` present
  - `device_name` shows actual GPU
- [ ] `compute.json` shows `device: cuda`

---

## Part C: Additional CLI Commands

### C1. Interactive Shell

```bash
jernerics shell --gpu 1
```

**Verify:**
- [ ] Opens interactive shell on HPC
- [ ] Inside container with GPU access
- [ ] Can run `python -c "import torch; print(torch.cuda.is_available())"` → `True`

---

### C2. Clean Remote

```bash
# Dry-run first
jernerics clean --all

# Confirm deletion looks correct
jernerics clean --all --force
```

**Verify:**
- [ ] Deletes `results/`, `.jernerics/logs/`, `container.sif` on remote

---

### C3. Cancel Jobs

```bash
jernerics cancel <job_id>
# OR
jernerics cancel --all
```

---

## Summary Checklist

| Part | Step | Command | Status |
|------|------|---------|--------|
| A | Local DAG | `jernerics run local` | [ ] |
| A | Container build | `jernerics container build` | [ ] |
| A | SLURM dry-run | `jernerics run slurm --dry-run` | [ ] |
| A | HPC submit | `jernerics run slurm` | [ ] |
| A | Job list | `jernerics jobs` | [ ] |
| A | View logs | `jernerics logs` | [ ] |
| A | Get results | `jernerics results` | [ ] |
| B | GPU container build | `jernerics container build` | [ ] |
| B | GPU dry-run | `jernerics run slurm --dry-run` | [ ] |
| B | GPU submit | `jernerics run slurm` | [ ] |
| B | GPU logs | `jernerics logs` | [ ] |
| B | GPU results | `jernerics results` | [ ] |
| C | Interactive shell | `jernerics shell` | [ ] |
| C | Clean remote | `jernerics clean` | [ ] |
| C | Cancel jobs | `jernerics cancel` | [ ] |

---

## Notes

- No local Docker/Apptainer required—all container testing happens on HPC
- `jernerics run slurm` auto-syncs code and checks for container.sif
- GPU test verifies CUDA availability and actual GPU computation
