---
name: jernerics
disable-model-invocation: true
description: |
  Use when writing or running experiments with jernerics — DAG-based
  experiment runner with multi-backend execution, hyperparameter sweeps,
  tracking, and artifact storage. Covers DAG authoring, config files,
  CLI commands, backend configuration, container starters, Optuna sweeps,
  tracking server, artifact upload, and retry system. Trigger on "dag",
  "sweep", "backend", "trial", "container", "tracking", "optuna",
  "apptainer", "docker", "slurm", "pueue", "artifact", "retry", or when
  working with dag.py / config.py files in a jernerics project.
---

# Jernerics

DAG-based experiment runner with multi-backend execution, hyperparameter
sweeps, and tracking.

**Before planning experiments or modifying config, load the relevant
reference doc below.** These contain current API details that may differ
from training data.

## Concept glossary

- **DAG** — A directed acyclic graph of tasks. Independent tasks run in
  parallel via thread pool.
- **Task** — A function decorated with `@task`. Receives dependency
  results by parameter name, plus `config` and optional `tracker`.
- **Sweep** — An Optuna-driven search over a `search_space` function.
  Each sample is a trial.
- **Trial** — One execution of the full DAG with specific
  hyperparameters.
- **Backend** — An execution target (local, Slurm, Pueue). Configured
  as named profiles in `pyproject.toml`.
- **Tracking** — Protobuf-encoded trial events streamed to a gRPC
  server backed by DuckDB.
- **Artifact storage** — S3-compatible object storage (MinIO) for trial
  artifacts, uploaded after each trial.
- **Retry** — Heartbeat-based staleness detection with automatic
  resubmission for node deaths.

## CLI surface

| Command | Description |
|---------|-------------|
| `jernerics init [dir]` | Create project scaffolding |
| `jernerics local <dag> <config>` | Run in-process (no backend) |
| `jernerics run -b <name> <dag> <config>` | Submit sweep to a backend |
| `jernerics build -b <name>` | Build container on remote |
| `jernerics jobs -b <name>` | List jobs |
| `jernerics cancel -b <name> [id]` | Cancel jobs |
| `jernerics logs -b <name> <id>` | View logs |
| `jernerics clean -b <name>` | Delete remote artifacts |
| `jernerics sync -b <name>` | Replay tracking data from remote |

Common flags: `--dry-run` (run/build), `--force` (init/build/clean),
`--follow` (logs), `--set KEY=VALUE` (run), `--study` (sync).

## Reference docs

Load these before relevant activities:

- **`references/project-setup.md`** — Adding jernerics to a project,
  pyproject.toml config, init command, container starters.
- **`references/dag-authoring.md`** — Tasks, dependency injection,
  tracker protocol, path handling, serial execution.
- **`references/backends.md`** — Backend profiles, container types,
  build/sync/clean commands, SSH hosts.
- **`references/tracking.md`** — Tracking server, artifact storage,
  environment variables, replay/sync.
- **`references/retry.md`** — Heartbeat, retry detection, retry
  config, failure modes, post-hook pipeline.
