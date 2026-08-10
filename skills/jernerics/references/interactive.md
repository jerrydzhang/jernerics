# Interactive GPU Sessions

`jernerics interactive` allocates a GPU node and drops you into a container
shell on it, for development and debugging outside a sweep. It is a Slurm-only
feature (Pueue/local backends are not supported).

## What it does

- Submits a Slurm reservation job (`sleep infinity`) that **survives SSH
  disconnect** — unlike `srun --pty`, your allocation persists if your
  connection drops.
- Connects to the compute node over SSH (`ProxyJump` through the login host;
  the cluster gates node SSH via `pam_slurm_adopt`, so an active job is
  required).
- Runs `apptainer shell`, landing you inside the container at `/work`.
- Starts **mutagen** for continuous, bidirectional code sync — local edits
  appear on the node within seconds, and vice-versa.

Process persistence (tmux, screen) is left to you. jernerics owns the
allocation and container entry, not your shell environment.

## Commands

```bash
jernerics interactive -b <name>                       # allocate + attach
jernerics interactive -b <name> --gpus 2 --time 4:00:00
jernerics interactive -b <name> --constraint a100
jernerics interactive -b <name> --dry-run             # show sbatch script + ssh cmd
jernerics interactive -b <name>                       # reconnect to a live allocation
jernerics interactive -b <name> --end                 # release the allocation
```

Re-running `interactive` reconnects to an existing allocation (and restarts a
dead sync session if needed). `--end` cancels the job and tears down the sync.

| Flag | Purpose |
|------|---------|
| `--backend`/`-b` | Backend name (Slurm) |
| `--time` | Walltime, e.g. `4:00:00` |
| `--gpus` | Number of GPUs (default 1) |
| `--partition` | Slurm partition |
| `--constraint` | Slurm constraint, e.g. `a100` |
| `--end` | Tear down the allocation + sync |
| `--dry-run` | Print the sbatch script and ssh command without running |

## Configuration

Optional defaults live under the backend's own table:

```toml
[tool.jernerics.backends.hpc.interactive]
time = "4:00:00"
gpus = 1
partition = "gpu"
constraint = "a100"
mem = "32G"
cpus = 8
```

Any field left unset inherits from the backend's
`[tool.jernerics.backends.<name>.slurm]` table, so interactive defaults track
the batch configuration. CLI flags override both.

## Code sync

Mutagen is an **optional** dependency. When present, edits flow both directions
in real time (the remote endpoint polls on NFS, where inotify doesn't fire;
local-side watching stays real time). When absent, jernerics falls back to a
single one-shot push (tar+scp) — the remote starts with current source but
won't receive later edits until you re-run the command.

Ignored paths: `__pycache__/`, `*.pyc`, `*.sif`, `results/`, `pools/`, `logs/`,
`.venv/`, build/dist caches. VCS dirs (`.git`) are ignored via mutagen's
`--ignore-vcs`.

## Prerequisites

- The container must already be built: run `jernerics build -b <name>` first.
- SSH key-based auth to the login host.
- For node SSH to work, the cluster must allow it for running jobs
  (`pam_slurm_adopt` or equivalent).
