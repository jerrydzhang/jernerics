---
name: jernerics
disable-model-invocation: true
description: |
  Use when writing or running experiments with jernerics — experiment
  runner with multi-backend execution, hyperparameter sweeps, tracking,
  and artifact storage. Covers trial authoring (trial_config/trial_tracker),
  config files, CLI commands, backend configuration, container starters,
  interactive GPU sessions, Optuna/grid sweeps, HTTP tracking server,
  artifact upload, and retry system. Trigger on "sweep", "backend", "trial",
  "container", "tracking", "optuna", "apptainer", "docker", "slurm", "pueue",
  "interactive", "artifact", "retry", "observability", "runs", "summary",
  or when working with trial.py / config.py files in a jernerics project.
---

# Jernerics

Experiment runner with multi-backend execution, hyperparameter sweeps,
and tracking.

**Before planning experiments or modifying config, load the relevant
reference doc below.** These contain current API details that may differ
from training data.

## Concept glossary

- **Trial** — A top-level script (`trial.py`) the user authors. It calls
  `trial_config()` for merged hyperparameters and `trial_tracker()` to record
  metrics/params/artifacts, then reports results via `tracker.finish(dict)`,
  which the `objective` lambda reads.
- **Sweep** — A search over the trial config: Optuna-sampled via a
  `search_space(trial)` function, or a deterministic `grid` (cartesian
  product). Each sample is one trial invocation.
- **Backend** — An execution target. Configured as named `slurm` or `pueue`
  profiles in `pyproject.toml`; `jernerics local` runs in-process with no
  backend.
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
| `jernerics interactive -b <name>` | Open a container shell on an allocated GPU node |
| `jernerics build -b <name>` | Build container on remote |
| `jernerics jobs -b <name>` | List jobs |
| `jernerics cancel -b <name> [id]` | Cancel jobs |
| `jernerics logs -b <name> <id>` | View logs |
| `jernerics wait -b <name> <id>` | Block until job completes |
| `jernerics clean -b <name>` | Delete remote artifacts |
| `jernerics sync -b <name>` | Replay tracking data from remote |
| `jernerics replay [--study <s>]` | Replay unsynced tracking data to server |
| `jernerics runs` | List runs from the tracking server |
| `jernerics summary <run>` | Per-metric analysis of one run |
| `jernerics diff <a> <b>` | Compare two runs (params + final metrics) |
| `jernerics trace <run> [metric]` | Show raw metric series for a run |

Common flags: `--dry-run` (run/build/interactive), `--force` (init/build/clean), `--follow` (logs), `--set KEY=VALUE` (run), `--study` (sync), `--end` (interactive), `--json` (runs/summary/diff/trace).

## Reference docs

Load these before relevant activities:

- **`references/project-setup.md`** — Adding jernerics to a project,
  pyproject.toml config, init command, container starters.
- **`references/trial-authoring.md`** — The `trial.py` script
  (`trial_config`/`trial_tracker`), tracker protocol, config handling,
  logging values/params/artifacts and reporting results via `finish()`.
- **`references/backends.md`** — Backend profiles, container types,
  build/sync/clean commands, SSH hosts.
- **`references/interactive.md`** — `interactive` command: GPU allocation,
  container shell, mutagen code sync, `InteractiveConfig`.
- **`references/tracking.md`** — Tracking server, artifact storage,
  environment variables, replay/sync.
- **`references/observability.md`** — `runs`/`summary`/`diff` commands,
  metric analysis (slopes), and when to use them vs raw SQL.
- **`references/retry.md`** — Heartbeat, retry detection, retry
  config, failure modes, post-hook pipeline.
