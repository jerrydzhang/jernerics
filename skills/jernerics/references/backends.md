# Backends

## Backend profiles

Named profiles in `pyproject.toml` under `[tool.jernerics.backends.*]`.

### Slurm

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
```

Sweeps become Slurm array jobs. Each trial is one array task.
Optuna study uses a shared SQLite database on the remote.
A post-hook job runs after the array completes (retry → sync).

### Pueue

```toml
[tool.jernerics.backends.pueue-remote]
type = "pueue"
host = "user@workstation.edu"
remote_dir = "~/projects/{project_name}"
container_type = "docker"

[tool.jernerics.backends.pueue-remote.pueue]
parallel = 2

[tool.jernerics.backends.pueue-local]
type = "pueue"
container_type = "none"
remote_dir = "."

[tool.jernerics.backends.pueue-local.pueue]
parallel = 2
```

Pueue manages a local task queue. Each trial is a pueue task. Omit `host`
for local-only pueue.

Sweeps run in a pueue group named after the study, limited to `parallel`
concurrent slots. The post-hook checker runs in its own
`<study>_checker` group pinned to `parallel = 1`, so it never competes with
trial slots. `job cancel` covers both groups.

## Container types

| `container_type` | Runtime | Build output |
|------------------|---------|--------------|
| `apptainer` | Apptainer/Singularity | `container.sif` |
| `docker` | Docker | Docker image (project name) |
| `none` | None | N/A — passthrough |

## Commands

```bash
# Build container on remote
jernerics backend build -b <name>
jernerics backend build -b <name> --force    # Force rebuild
jernerics backend build -b <name> --dry-run  # Preview

# Submit sweep
jernerics run -b <name> trial.py config.py
jernerics run -b <name> trial.py config.py --dry-run
jernerics run -b <name> trial.py config.py --set time=4:00:00     # scheduler override
jernerics run -b <name> trial.py config.py --set-param lr=0.01    # trial-config param

# Monitor
jernerics job list -b <name>
jernerics job list -b <name> --all
jernerics job logs -b <name> <id> --follow
jernerics job logs -b <name> <id> --array-index 3   # one trial of an array job
jernerics job logs -b <name> <id> --stderr
jernerics job resources <id>          # scheduler accounting (sacct / pueue)
jernerics job resources <id> --ship   # also append the record to the tracking server

# Cancel
jernerics job cancel -b <name> <id>
jernerics job cancel -b <name> --all
```

`--set` keys are validated against the target backend's override key set —
an unknown key errors immediately instead of being silently ignored. Use
`--set-param` for trial-config params, not scheduler options.

> **Warning (Slurm):** `job cancel` cancels the main array job but leaves the
> checker dependency job pending. Clean up with `jernerics job cancel -b <name> --all`
> or manually `scancel` all pending jobs for your user. (Pueue cancel covers
> the checker group too.)

```bash
# Clean remote artifacts
jernerics backend clean -b <name>
jernerics backend clean -b <name> --full --force

# Pull tracking data from the backend and ship it to the server
jernerics tracking replay -b <name>
jernerics tracking replay -b <name> --study <name>
```

## SSH hosts

Remote backends use SSH. Ensure key-based auth is configured.

For Slurm: the adapter runs `sbatch`, `sacct`, `scancel` via SSH.

For Pueue: the adapter runs `pueue` CLI commands via SSH.

## Tilde expansion

- SSH commands: `~` expands. Use directly.
- Slurm directives, quoted strings: `~` does NOT expand. Use `$HOME`.

## Project-source exclusions

Every mechanism that transfers project source to the remote (deployment tar
sync, interactive mutagen sync, one-shot fallback) applies one policy:
`.gitignore` patterns, then `.jernericsignore` patterns, then a built-in
list (`__pycache__/`, `*.pyc`, `*.sif`, `results/`, `pools/`, `logs/`,
`.venv/`, caches — built-ins always win). Add a project-root
`.jernericsignore` (Git syntax) for files that must not synchronize even
when Git-tracked or not Git-ignored. Files excluded by the policy (e.g.
`.pkl` data pools you re-include deliberately) must be copied manually via
`scp`. If a referenced file doesn't exist on the remote, jobs fail
instantly and retry indefinitely.
