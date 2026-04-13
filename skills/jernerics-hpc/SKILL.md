---
name: jernerics-hpc
description: |
  Use when building containers, configuring HPC infrastructure, debugging
  cluster issues, setting up bind mounts, or handling SSH/SLURM operations
  for jernerics. Covers pyproject.toml HPC config, container definitions,
  path handling gotchas (tilde expansion), persistent caches, and the
  internal HPC/SSH/Sync/SLURM APIs. Trigger on "container build", "bind
  mount", "SLURM", "SSH", "cluster config", or when debugging HPC-specific
  failures.
---

# Jernerics HPC Infrastructure

Infrastructure and operations for running jernerics experiments on HPC clusters via SLURM + Apptainer.

## pyproject.toml Configuration

All HPC config lives under `[tool.jernerics]` in `pyproject.toml`:

```toml
# Required: HPC connection
[tool.jernerics.hpc]
host = "user@cluster.edu"                          # SSH host (or set JERNERICS_HPC_HOST env var)
remote_dir = "~/projects/{project_name}"           # Remote project directory
cache_dir = "/scratch/$USER/jernerics"             # Optional: scratch storage for binds

# Optional: Default SLURM settings for container builds
[tool.jernerics.container]
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4

# Optional: Safety limits
[tool.jernerics.safety]
max_concurrent_jobs = 10

# Optional: Interactive shell defaults
[tool.jernerics.shell]
partition = "priority-gpu"
cpus = 4
mem = "32G"
gpu = 1
time = "2:00:00"

# Optional: Persistent directory binds
[tool.jernerics.binds]
"/work/.julia_env" = "julia_env"
"/work/.julia_depot" = "julia_depot"
"/work/checkpoints" = "checkpoints"
```

**Minimal config:** Only `[tool.jernerics.hpc]` with `host` is required. Everything else has defaults.

**`{project_name}`** in `remote_dir` and `cache_dir` is replaced with the project name from `pyproject.toml`.

**Host override:** The `JERNERICS_HPC_HOST` environment variable overrides the `host` setting.

### Config loading

`load_jernerics_config(project_dir)` returns `(HpcConfig, ShellConfig, BindsConfig)`:

- `HpcConfig`: host, remote_dir, partition, time, mem, cpus, max_concurrent_jobs, cache_dir
- `ShellConfig`: partition, cpus, mem, gpu, time (all optional, used by `jernerics shell`)
- `BindsConfig`: dict mapping container_path → cache_subdir name

The `container` section feeds into both `HpcConfig` (partition/time/mem/cpus for builds) and `safety` section for `max_concurrent_jobs`.

## Container Building

### When to build

| Change Type | Rebuild? | Why |
|-------------|----------|-----|
| Source code | No | Source is bind-mounted at runtime |
| Config changes | No | Config is passed at runtime |
| `pyproject.toml` dependencies | Yes | Dependencies baked into container |
| `uv.lock` changes | Yes | Lockfile determines installed packages |
| `container.def` changes | Yes | Definition changed |

### Build command

```bash
jernerics container build            # Build if stale
jernerics container build --force    # Force rebuild
jernerics container build --dry-run  # Preview
```

**Prerequisites:** `uv.lock` must exist. `container.def` is auto-generated if missing.

The build:
1. Syncs project to remote via tarball
2. Creates logs directory on remote
3. Optionally creates tmpdir in cache_dir for Apptainer
4. Submits SLURM build job via `sbatch`
5. Saves job metadata to `.jernerics/jobs/<job_id>.json`

### Container definition file

`container.def` is an Apptainer definition file. Default template (`python`):

```
Bootstrap: docker
From: python:3.12-slim

%files
    pyproject.toml /build/project/pyproject.toml
    uv.lock /build/project/uv.lock
    src/ /build/project/src/

%post
    pip install uv
    cd /build/project
    export UV_PROJECT_ENVIRONMENT=/opt/venv
    uv sync --frozen --no-dev
    rm -rf /build /root/.cache

%environment
    export PATH="/opt/venv/bin:$PATH"
    export PYTHONPATH="/work/src"
```

Key points:
- Dependencies installed via `uv sync --frozen` into `/opt/venv`
- `PYTHONPATH=/work/src` so source code is importable from bind mount
- Source is NOT baked in — it's bind-mounted at `/work` at runtime

Available templates: check `list_templates()` or `ls src/jernerics/templates/`.

## Path Handling: Tilde Expansion

**This is the #1 source of bugs in HPC code.**

### `~` DOES expand (use directly)

SSH commands via `_quote_path()` — the remote shell expands it:

```python
from jernerics.hpc.ssh import _quote_path

ssh.mkdir("~/projects/foo")  # Works — _quote_path preserves ~
```

### `~` DOES NOT expand (use `$HOME` instead)

- SLURM `--output`/`--error` directives (not processed by shell)
- Double-quoted strings in bash scripts
- Paths embedded in heredocs or inline scripts

**Wrong (SLURM directive):**
```python
f"#SBATCH --output={remote_dir}/build_%j.out"  # remote_dir = "~/projects/foo"
# ~ is literal in SBATCH directives
```

**Correct:**
```python
slurm_dir = remote_dir.replace("~", "$HOME")
f"#SBATCH --output={slurm_dir}/build_%j.out"  # "$HOME/projects/foo"
```

**Wrong (bind path in quotes):**
```python
f'"{cache_path}:{container_path}"'  # cache_path = "~/cache"
```

**Correct:**
```python
cache_path = cache_path.replace("~", "$HOME")
f'"{cache_path}:{container_path}"'  # "$HOME/cache:/work/cache"
```

### `_quote_path` internals

`_quote_path(path)` in `src/jernerics/hpc/ssh.py`:
- If path starts with `~`, returns `~` + `shlex.quote(rest)` — preserving `~` for shell expansion
- Otherwise, returns `shlex.quote(path)`

## Bind Mounts for Persistence

### The problem

Inside the container:
- `/work` is the project root (bind-mounted, writable, persisted)
- Paths like `.venv/` are built into the container image — read-only at runtime
- `~/.cache`, `~/.julia`, etc. are ephemeral — data lost between runs

Libraries that try to write to these locations fail or lose data.

### The solution: `cache_dir` + `binds`

1. **Configure cache location:**
```toml
[tool.jernerics.hpc]
cache_dir = "/scratch/$USER/jernerics"
```

2. **Define bind mappings:**
```toml
[tool.jernerics.binds]
"/work/.julia_env" = "julia_env"       # container_path = cache_subdir
"/work/checkpoints" = "checkpoints"
```

This creates:
- On HPC: `/scratch/$USER/jernerics/<project>/julia_env` → mounted at `/work/.julia_env`
- Locally: `~/.cache/jernerics/<project>/julia_env` (for testing)

3. **Use in code:**
```python
from jernerics.paths import bind

julia_env = bind("julia_env")  # Path("/work/.julia_env") on HPC
```

`bind()` raises `BindNotFound` if the name isn't configured.

**Warning:** Files in `cache_dir` are temporary — use for caches and checkpoints, not permanent storage.

## SSH Client API

`SSHClient` wraps SSH operations:

```python
from jernerics.hpc import SSHClient

ssh = SSHClient("user@cluster.edu")

ssh.run("command", capture_output=True, check=True, timeout=30)
ssh.run_script(script_content, check=True, timeout=60)
ssh.mkdir("~/projects/foo")
ssh.file_exists("~/projects/foo/container.sif")
ssh.getmtime("~/projects/foo/container.sif")  # → float or None
ssh.remove_file("~/projects/foo/old.tar.gz")
ssh.get_home_dir()        # → "/home/user"
ssh.expand_tilde("~/foo") # → "/home/user/foo"
```

All path-taking methods use `_quote_path()` internally (tilde-safe).

## File Syncer API

`FileSyncer` syncs project files to remote:

```python
from jernerics.hpc import FileSyncer, SSHClient

ssh = SSHClient("user@cluster.edu")
syncer = FileSyncer(ssh, "~/projects/my-project")

syncer.sync_project("/path/to/local/project")  # Full sync via tarball
syncer.sync_file("local_file.txt")             # Single file
syncer.download_file("~/remote/results.json", "local.json")
syncer.container_exists()                      # Check container.sif
syncer.container_needs_rebuild("uv.lock")      # Compare timestamps
```

**Excludes** (not synced): `.git/`, `.jernerics/`, `__pycache__/`, `.venv/`, `results/`, `.cache/`, `*.pyc`, `*.sif`, plus anything in `.gitignore`.

## SLURM Job Manager API

```python
from jernerics.hpc import SlurmJobManager, SSHClient

ssh = SSHClient("user@cluster.edu")
slurm = SlurmJobManager(ssh)

job_id = slurm.submit("path/to/script.sh")          # Submit from file
job_id = slurm.submit_inline(script_content, workdir="~/project")  # Inline

jobs = slurm.list_jobs()                             # Running jobs
jobs = slurm.list_jobs(include_completed=True)       # All jobs

slurm.cancel("12345")
slurm.cancel_all()

status = slurm.get_status("12345")                   # → "RUNNING", "COMPLETED", etc.
success = slurm.wait_for_completion("12345", poll_interval=30, timeout=3600)

path = slurm.get_job_output_path("12345", "logs/%A_%a.out")
```

### SLURM filename patterns

`expand_slurm_pattern(pattern, job_id, array_task_id, ...)` supports: `%j` (job ID), `%A` (array master ID), `%a` (array task ID), `%x` (job name), `%u` (username), `%N` (node).

### Array job concurrency

In the config file's `slurm` dict, `max_parallel` controls how many array tasks run simultaneously:

```python
slurm = {"max_parallel": 4, ...}  # At most 4 configs run in parallel
```

Defaults to `max_concurrent_jobs` from `[tool.jernerics.safety]` (default: 10).

## Execution Flow (run slurm)

When `jernerics run slurm dag.py config.py` is called:

1. Load pyproject.toml config, DAG file, config file
2. Build SLURM script with inline Python runner
3. Sync project to remote via `FileSyncer`
4. Create log directory on remote
5. Create cache directories for binds (if configured)
6. Verify `container.sif` exists on remote
7. Submit script via `sbatch --parsable`
8. Save job metadata to `.jernerics/jobs/<job_id>.json`

The inline runner in the SLURM script:
- Sets `JERNERICS_HPC=1`, `JERNERICS_DAG_FILE=/work/<dag>`, `JERNERICS_CONFIG_FILE=/work/<config>`
- Runs `apptainer exec --fakeroot --contain --nv --pwd /work --bind ...` with the container
- Inside the container: loads the DAG, runs config by index, reports success/failure

### Runtime environment variables

Inside the container on HPC:
- `JERNERICS_HPC=1`
- `JERNERICS_DAG_FILE=/work/<relative_dag_path>`
- `JERNERICS_CONFIG_FILE=/work/<relative_config_path>`
- `JERNERICS_CONFIG_INDEX=<0-based index>`
- `PYTHONPATH=/work/src`

## Clean

```bash
jernerics clean --dry-run              # Preview (default)
jernerics clean --results --logs --force  # Delete specific artifacts
jernerics clean --all --force          # Delete everything on remote
```

Deletes on remote: `results/`, `.jernerics/logs/`, `container.sif`. Requires `--force` to actually delete.

## Common HPC Issues

| Issue | Cause | Fix |
|-------|-------|-----|
| Container build OOM | `mem` too small for pip/uv | Increase `[tool.jernerics.container] mem` |
| Build fails: `uv.lock` not found | Lockfile missing | Run `uv lock` locally first |
| `BindNotFound` at runtime | Bind name not in config | Add to `[tool.jernerics.binds]` |
| `~` in SLURM output path | Tilde not expanded by SLURM | Code handles this, but check if writing custom scripts |
| Source changes not reflected | Stale container | Source is bind-mounted — shouldn't happen. Check sync. |
| Permission denied on `/work/.venv` | Writing to read-only container path | Use `bind()` for writable dirs |
