---
name: jernerics
disable-model-invocation: true
description: |
  Use when writing or running experiments with jernerics — experiment
  runner with multi-backend execution, hyperparameter sweeps, tracking,
  and artifact storage. Covers trial authoring (trial(config, tracker)),
  config files, CLI commands, backend configuration, container starters,
  Optuna sweeps, HTTP tracking server, artifact upload, and retry system.
  Trigger on "sweep", "backend", "trial", "container", "tracking",
  "optuna", "apptainer", "docker", "slurm", "pueue", "artifact", "retry",
  "observability", "runs", "summary", "diff",
  or when working with trial.py / config.py files in a jernerics project.
---

# Jernerics

Experiment runner with multi-backend execution, hyperparameter sweeps,
and tracking.

**Before planning experiments or modifying config, load the relevant
reference doc below.** These contain current API details that may differ
from training data.

## Concept glossary

- **Trial** — A `trial(config, tracker)` function authored by the user.
  `config` holds merged hyperparameters; `tracker` records metrics/params/
  results/artifacts. Returns a dict read by the `objective` lambda.
- **Sweep** — An Optuna-driven search over a `search_space` function.
  Each sample is one trial invocation.
- **Backend** — An execution target (local, Slurm, Pueue). Configured
  as named profiles in `pyproject.toml`.
- **Tracking** — An HTTP server that ingests trial events (JSONL over
  HTTP `/ingest`) and serves them via SQL (`/query`). Metrics stream live
  during the run; a final replay guarantees delivery.
- **Artifact storage** — Artifact files served from the tracking
  server's disk over HTTP (`/artifact`); no external object storage.
- **Retry** — Heartbeat-based staleness detection with automatic
  resubmission for node deaths.

## CLI surface

| Command | Description |
|---------|-------------|
| `jernerics init [dir]` | Create project scaffolding |
| `jernerics local <trial> <config>` | Run in-process (no backend) |
| `jernerics run -b <name> <trial> <config>` | Submit sweep to a backend |
| `jernerics build -b <name>` | Build container on remote |
| `jernerics jobs -b <name>` | List jobs |
| `jernerics cancel -b <name> [id]` | Cancel jobs |
| `jernerics logs -b <name> <id>` | View logs |
| `jernerics clean -b <name>` | Delete remote artifacts |
| `jernerics sync -b <name>` | Replay tracking data from remote |
| `jernerics runs` | List runs from the tracking server |
| `jernerics summary <run>` | Per-metric analysis of one run |
| `jernerics diff <a> <b>` | Compare two runs (params + final metrics) |

Common flags: `--dry-run` (run/build), `--force` (init/build/clean),
`--follow` (logs), `--set KEY=VALUE` (run), `--study` (sync), `--json` (runs/summary/diff).

## Reference docs

Load these before relevant activities:

- **`references/project-setup.md`** — Adding jernerics to a project,
  pyproject.toml config, init command, container starters.
- **`references/trial-authoring.md`** — The `trial(config, tracker)` function,
  tracker protocol, config handling, logging metrics/params/results/artifacts.
- **`references/backends.md`** — Backend profiles, container types,
  build/sync/clean commands, SSH hosts.
- **`references/tracking.md`** — Tracking server, artifact storage,
  environment variables, replay/sync.
- **`references/observability.md`** — `runs`/`summary`/`diff` commands,
  metric analysis (slopes), and when to use them vs raw SQL.
- **`references/retry.md`** — Heartbeat, retry detection, retry
  config, failure modes, post-hook pipeline.
