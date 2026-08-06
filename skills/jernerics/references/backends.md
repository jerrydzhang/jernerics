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
parallel = 2
container_type = "docker"

[tool.jernerics.backends.pueue-local]
type = "pueue"
parallel = 2
container_type = "none"
remote_dir = "."
```

Pueue manages a local task queue. Each trial is a pueue task.
Omit `host` for local-only pueue.

## Container types

| `container_type` | Runtime | Build output |
|------------------|---------|--------------|
| `apptainer` | Apptainer/Singularity | `container.sif` |
| `docker` | Docker | Docker image (project name) |
| `none` | None | N/A — passthrough |

## Commands

```bash
# Build container on remote
jernerics build -b <name>
jernerics build -b <name> --force    # Force rebuild
jernerics build -b <name> --dry-run  # Preview

# Submit sweep
jernerics run -b <name> trial.py config.py
jernerics run -b <name> trial.py config.py --dry-run
jernerics run -b <name> trial.py config.py --set time=4:00:00

# Monitor
jernerics jobs -b <name>
jernerics jobs -b <name> --all
jernerics logs -b <name> <id> --follow

# Cancel
jernerics cancel -b <name> <id>
jernerics cancel -b <name> --all
```

> **Warning:** `cancel` cancels the main array job but leaves the checker dependency
> job pending. Clean up with `jernerics cancel -b <name> --all` or manually `scancel`
> all pending jobs for your user.

```bash
# Clean remote artifacts
jernerics clean -b <name>
jernerics clean -b <name> --full --force

# Sync tracking data from remote
jernerics sync -b <name>
jernerics sync -b <name> --study <name>
```

## SSH hosts

Remote backends use SSH. Ensure key-based auth is configured.

For Slurm: the adapter runs `sbatch`, `sacct`, `scancel` via SSH.

For Pueue: the adapter runs `pueue` CLI commands via SSH.

## Tilde expansion

- SSH commands: `~` expands. Use directly.
- Slurm directives, quoted strings: `~` does NOT expand. Use `$HOME`.

## Non-tracked files

jernerics syncs git-tracked files to the remote automatically. Gitignored files
(e.g. `.pkl` data pools) must be synced manually via `scp`. If a referenced file
doesn't exist on the remote, jobs fail instantly and retry indefinitely.
