# HPC CLI Design Plan

## Goal

Create a CLI-driven workflow for running experiments on HPC (SLURM + Apptainer) that:
1. Requires minimal setup per project (no copying scripts)
2. Works for agents without risk of getting banned
3. Supports GPU and CPU Python projects
4. Is reproducible via git-pinned dependencies and uv.lock

## Current State (Working)

The GPU example is working at `examples/container-gpu/`:

```
LOCAL                          HPC
test_sync_run.sh ─────────────► receives files via tar/ssh
                               submits SLURM build job
                                        │
                               apptainer build container.sif
                               (downloads jernerics from git, torch, etc)
                                        │
run.sh (on login node) ───────► apptainer exec container.sif
                                        │
                               jernerics run slurm dag.py config.py
                                        │
                               SLURM experiment job runs DAG
```

**Key files:**
- `container.def` - Apptainer definition with `PYTHONPATH=/work/src`
- `pyproject.toml` - Dependencies, jernerics pinned to git commit
- `uv.lock` - Lockfile for reproducibility
- `test_sync_run.sh` - Syncs files, submits build job to SLURM
- `run.sh` - Runs on login node, calls `jernerics run slurm`

**Verified working:**
- GPU detection (RTX 2080 Ti, CUDA 12.1)
- Full DAG workflow: detect_gpu → run_compute → finalize

## Proposed Design

### Command Set

```bash
# Primary workflow (from local machine)
jernerics container build              # Sync + build container on HPC
jernerics container build --force      # Force rebuild
jernerics container build --dry-run    # Preview without executing

jernerics run slurm dag.py config.py   # Sync + submit experiment to HPC
jernerics run slurm ... --dry-run      # Preview SLURM script

jernerics jobs                          # List my jobs
jernerics jobs --all                    # Include completed
jernerics cancel <job_id>               # Cancel job
jernerics cancel --all                  # Cancel all my jobs

jernerics logs <job_id>                 # View job output
jernerics logs <job_id> --follow        # Tail -f output
jernerics results <job_id>              # Download results

jernerics shell                         # Interactive compute node with GPU
jernerics shell --gpu=2 --time=2:00:00  # Custom resources

jernerics clean --results               # Delete old results
jernerics clean --container             # Delete container.sif

# Local testing (already works)
jernerics run local dag.py config.py
```

### Project Structure (minimal)

```
my-project/
├── pyproject.toml    # Dependencies + [tool.jernerics] config
├── uv.lock           # Lockfile (auto-generated)
├── dag.py            # Experiment DAG
├── config.py         # Config + SLURM settings
└── src/              # Project code
```

No `container.def`, `test_sync_run.sh`, or `run.sh` needed - CLI generates them.

### Configuration (pyproject.toml)

```toml
[project]
name = "my-project"
dependencies = [
    "torch",
    "jernerics",
]

[tool.uv.sources]
jernerics = { git = "https://github.com/jerrydzhang/jernerics.git", rev = "COMMIT_HASH" }

[tool.jernerics]
hpc_host = "jez21005@hpc2.storrs.hpc.uconn.edu"
remote_dir = "~/experiments/{project_name}"

[tool.jernerics.container]
template = "gpu"  # "gpu" or "cpu"
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4

[tool.jernerics.safety]
max_concurrent_jobs = 10
require_confirmation = ["clean"]
```

### Templates

Two templates stored in `src/jernerics/templates/`:

**gpu.def:**
```
Bootstrap: docker
From: python:3.12-slim

%files
    pyproject.toml /build/project/pyproject.toml
    uv.lock /build/project/uv.lock
    src/ /build/project/src/

%post
    apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
    pip install uv
    cd /build/project
    export UV_PROJECT_ENVIRONMENT=/opt/venv
    uv sync --frozen --no-dev
    rm -rf /build /root/.cache

%environment
    export PATH="/opt/venv/bin:$PATH"
    export PYTHONPATH="/work/src"
    export LANG=C.UTF-8
    export LC_ALL=C.UTF-8

%runscript
    exec "$@"
```

**cpu.def:** Same but without GPU-related packages in pyproject.toml

### Agent Safety

| Category | Operations | Behavior |
|----------|-----------|----------|
| Safe | status, logs, list, cancel | Always allowed |
| Limited | sync source, submit jobs | Within quotas |
| Expensive | build container | `--force` to rebuild |
| Destructive | delete results/container | `--force` + confirmation |

**Protections:**
- All SSH scoped to `remote_dir` and `/tmp`
- No compute on login node (always via SLURM)
- `max_concurrent_jobs` check before submit
- `--dry-run` for preview mode

### Key Design Decisions

1. **Always SSH from local** - No support for running on HPC login node directly. If you're there manually, use raw `sbatch`/`apptainer`.

2. **Auto-detect rebuilds** - `jernerics container build` only rebuilds if:
   - Container doesn't exist
   - `uv.lock` is newer than container
   - `--force` flag used

3. **jernerics pinning** - User manages version in pyproject.toml. CLI doesn't auto-update.

4. **Interactive debugging** - `jernerics shell` gives compute node with GPU, not login node.

5. **No external registries** - Container built on HPC, not pushed to ghcr.io or Docker Hub.

## Implementation Plan

### Phase 1: Container Commands

1. Add `[tool.jernerics]` config parsing to CLI
2. Create `jernerics container build` command:
   - Read config from pyproject.toml
   - Generate container.def from template
   - Sync files to HPC via rsync/tar
   - Submit SLURM build job
   - Poll for completion
3. Create templates: `gpu.def`, `cpu.def`
4. Add `--force` and `--dry-run` flags

### Phase 2: Run Commands

1. Modify `jernerics run slurm` to:
   - SSH to HPC
   - Sync changed files
   - Submit via remote sbatch
2. Add `--dry-run` flag
3. Add job management: `jobs`, `cancel`, `logs`, `results`

### Phase 3: Shell & Cleanup

1. Implement `jernerics shell` using `srun --pty`
2. Implement `jernerics clean` with safety checks

### Phase 4: Polish

1. Error handling and user-friendly messages
2. Progress indicators for long operations
3. `jernerics init` command for new projects

## Files to Create/Modify

```
src/jernerics/
├── cli.py                    # Add container, jobs, cancel, logs, shell commands
├── _cli_helpers.py           # Config parsing, SSH helpers
├── container/
│   ├── __init__.py
│   ├── builder.py            # Container build logic
│   └── templates.py          # Template generation
├── hpc/
│   ├── __init__.py
│   ├── ssh.py                # SSH operations
│   ├── slurm.py              # SLURM job management
│   └── sync.py               # File sync
└── templates/
    ├── gpu.def
    └── cpu.def
```

## Testing

1. Unit tests for config parsing, template generation
2. Integration test against mock SSH server
3. Manual test on real HPC with GPU example

## Open Questions

None - design is complete. Ready for implementation.
