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
- **Tracking** — An HTTP server that ingests tagged events (JSONL over
  HTTP `/ingest`) and serves typed domain reads, a read-only dashboard,
  and raw SQL (`/query`). Metrics stream live during the run; a final
  replay guarantees delivery.
- **Artifact storage** — Immutable artifact blobs with versions by
  repeated key, served from the tracking server's disk over HTTP
  (`/artifact/{id}`); no external object storage.
- **Retry** — Heartbeat-based staleness detection with automatic
  resubmission for node deaths; retries carry lineage so families read
  as generations.

## CLI surface

| Command | Description |
|---------|-------------|
| `jernerics init [dir]` | Create project scaffolding |
| `jernerics local <trial> <config>` | Run in-process (no backend) |
| `jernerics run -b <name> <trial> <config>` | Submit sweep to a backend |
| `jernerics interactive start -b <name>` | Open or reconnect a container shell on an allocated GPU node |
| `jernerics interactive stop -b <name>` | Tear down the interactive session and its sync |
| `jernerics interactive sync status -b <name>` | Report-only sync session state (connectivity, conflicts) |
| `jernerics interactive sync resolve <path>... -b <name> --from local\|cluster` | Safely resolve sync conflicts (backups + checksums) |
| `jernerics backend build -b <name>` | Build container on remote |
| `jernerics backend clean -b <name>` | Delete remote artifacts |
| `jernerics job list -b <name>` | List jobs |
| `jernerics job cancel -b <name> [id]` | Cancel jobs |
| `jernerics job logs -b <name> <id>` | View logs |
| `jernerics job wait -b <name> <id>` | Block until job completes |
| `jernerics tracking runs` | List this project's trials with derived monitoring |
| `jernerics tracking summary <ref>` | One trial: lineage, params, values, artifacts, executions |
| `jernerics tracking diff <a> <b>` | Compare two trials (params + latest values) |
| `jernerics tracking trace <ref> <key>` | One value key's step series |
| `jernerics tracking query "<sql>"` | Raw read-only SQL escape hatch |

Common flags: `--dry-run` (run/backend build/interactive start/tracking replay/resolve), `--force` (init/backend build/backend clean), `--follow` (job logs), `--set KEY=VALUE` (run), `--study` (tracking replay), `--json` (tracking commands, interactive sync status). A trial ref is `<sweep-name>:<trial-number>` or a 32-hex trial id.

## Reference docs

Load these before relevant activities:
- **`references/project-setup.md`** — Adding jernerics to a project,
  pyproject.toml config, init command, container starters.
- **`references/trial-authoring.md`** — The `trial.py` script
  (`trial_config`/`trial_tracker`), tracker protocol, config handling,
  logging values/params/artifacts and reporting results via `finish()`.
- **`references/backends.md`** — Backend profiles, container types,
  build/clean commands, project-source exclusions (`.jernericsignore`),
  SSH hosts.
- **`references/interactive.md`** — `interactive start`/`stop`: GPU
  allocation, container shell, mutagen code sync, `interactive sync
  status`/`resolve`, `InteractiveConfig`.
- **`references/tracking.md`** — Tracking server, event model, artifact
  storage, environment variables, `tracking replay`.
- **`references/observability.md`** — `tracking runs`/`summary`/`diff`/
  `trace`/`query` commands and when to use them vs raw SQL.
- **`references/retry.md`** — Heartbeat, retry detection, retry
  config, failure modes, post-hook pipeline.
