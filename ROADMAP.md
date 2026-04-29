# Jernerics Roadmap

Share URL:
https://pi.dev/session/#01f302a5d24f41f247c174642407b4f7
Gist:
https://gist.github.com/jerrydzhang/01f302a5d24f41f247c174642407b4f7

Living document. Updated as decisions are made and phases complete.

## Project Direction

This branch is a ground-up rewrite of the codebase. The goal is full ownership — every line understood, no black boxes, no code that survives because it works and nobody knows why. The original prototype was vibe-coded and it shows: duplicated logic, unclear boundaries, and dependencies that paper over problems instead of solving them. This branch fixes that properly.

The project serves two purposes and both matter:

1. **A tool I actually use.** A lean experiment runner for HPC — DAG scheduling, sweep optimization, result tracking — without the overhead of heavy frameworks. The main branch works for this today. This branch will replace it when it's done.
2. **A project I enjoy working on.** This is weekend/hobby work. The process matters as much as the output. Understanding why things work, building things simply, and ending up with code I could explain to someone line by line.

### Branch mechanics

This is not a ship-fast branch. It merges when it's done. Intermediate states don't need to be self-consistent or usable — the main branch exists for real work. This means:

- It's fine to break things and fix them later.
- It's fine to leave subsystems incomplete between sessions.
- The only constraint is that we can test when we need to.
- No pressure to maintain a working state at every commit.

---

## Completed: Phase 1 — Executor Rewrite

Replaced the old dual-path executor (serial + thread with duplicated logic) with a unified design.

- **Runner protocol.** `execute_dag` takes `runner=` implementing the `Runner` protocol. `ThreadPoolRunner` for production, `SyncRunner` for debugging. Users instantiate in config file.
- **TaskResult.** Every task returns `TaskResult(value, error, error_traceback)`. No more mixing values and exceptions in result dicts.
- **FIRST_COMPLETED.** Fixed the core scheduling bug where fast tasks' downstream work was blocked by slow tasks.
- **Unregistered dependencies raise.** Broken DAG, not a warning.
- **Loop guard.** `if not ready_task_names and not futures: break` — exit only when nothing ready AND nothing in flight.

---

## Completed: Phase 2 — Extract the Runner Module

Replaced `_get_sweep_runner_code()` f-string in `cli.py` with a real importable module.

- **`src/jernerics/runner.py`.** `run_trial()` is a real function, importable and testable. Invoked via `python -m jernerics.runner` with argparse CLI args.
- **Study lifecycle separation.** Caller creates the Optuna study; runner loads it via `optuna.load_study()`. No more double-creation or race condition noise.
- **`_base` → `base`.** Config variable renamed — no reason for the underscore prefix.
- **`objective_task`/`objective_metric` → `objective`.** Single callable instead of two string keys. Handles nested access, aggregation, and structured results.
- **All output to stderr.** Runner progress and error messages go to stderr; stdout is clean for future programmatic use.
- **`_generate_sweep_script()` updated.** Bash script invokes `python -m jernerics.runner` instead of embedding a heredoc with f-string Python.
- **`_get_sweep_runner_code()` deleted.**

### Additional changes
- Deleted `_get_mlflow_sync_script()` and `mlflow sync` command.
- Removed all MLflow env vars from SLURM script generation and `run local`.
- Removed `MlflowConfig`, `mlflow` dependency, `mlflow-export-import` dependency.
- Removed `active_run_id` from `__init__.py`.
- `load_jernerics_config()` now returns 3-tuple (was 4-tuple with MlflowConfig).

---

## Completed: Phase 3a — MLflow Removal

All MLflow code deleted. No replacement tracking yet — Optuna SQLite stores params + objectives for now.

- [x] Delete: `MlflowConfig`, `mlflow sync` command, env vars in SLURM scripts, `mlflow_export_import` dependency
- [x] Update `__init__.py` — remove `active_run_id`

---

## Completed: Phase 3b — Custom Tracking

Replaced MLflow with a lightweight tracking layer + marimo dashboard.

- [x] Design run directory structure and schema
- [x] Create `tracking/tracker.py` — `Tracker` class with `log_param()`, `log_metric()`, `log_result()`, `log_artifact()`
- [x] Integrate tracker into runner, DAG executor, and CLI
- [x] Monorepo refactor — split into three packages with uv workspace
- [x] Build gRPC server with DuckDB backend
- [x] Create sync client (`client.py`) — background thread sends events to server via gRPC
- [x] `jernerics sync` CLI command — replay orphaned .pb files to server

---

## Completed: Phase 4 — Cache Paths + Sync

- [x] Add `paths.cache_dir()` — resolves to `~/.cache/jernerics/<project>` locally, `hpc_config.cache_dir` on HPC
- [x] Eliminate `use_scratch` branching in `_generate_sweep_script()` — container always sees `/work` + `/cache`
- [x] Update `run_local` to use `cache_dir()` for optuna and tracking directories
- [x] Add `jernerics sync` command — replays .pb files to tracking server via apptainer exec on HPC
- [x] Add `tracking/data_sync.py` — concurrent replay with retry logic
- [x] Add `tracking/replay_runner.py` — `__main__` entry point invoked on HPC
- [x] Rename `hpc/` → `backend/` (source and tests)

---

## Completed: Phase 5a — Multi-Backend Config + CLI Rewrite

Config schema and CLI rewritten for multi-backend execution. Named backends, `--backend` flag on all remote commands.

### Config schema

```toml
[tool.jernerics]
tracking_server = "hostname:50051"     # optional, global

[tool.jernerics.backends.hpc]          # user-chosen name
type = "slurm"
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}"
cache_dir = "/scratch/$USER/jernerics"  # optional
partition = "priority"
time = "1:00:00"
mem = "16G"
cpus = 4
max_concurrent_jobs = 10
```

### CLI commands

| Command | Notes |
|---------|-------|
| `jernerics init` | Generates new config schema |
| `jernerics run --backend <name> [dag] [config]` | Sync + submit sweep |
| `jernerics run local [dag] [config]` | No backend needed, blocking |
| `jernerics build --backend <name>` | Sync + submit container build |
| `jernerics jobs --backend <name>` | List jobs |
| `jernerics cancel --backend <name>` | Cancel jobs |
| `jernerics logs --backend <name> <id>` | View/follow logs |
| `jernerics clean --backend <name>` | Clean remote artifacts |
| `jernerics sync --backend <name>` | Replay tracking data |

### What changed
- [x] New config: `BackendConfig` replaces `HpcConfig`, `ShellConfig`, `BindsConfig`
- [x] New config: `load_backend_config(name)` + `load_tracking_server()` replace `load_jernerics_config()`
- [x] New config: `backends` section with named entries, `tracking_server` at global level
- [x] CLI: `--backend` flag on all remote commands (required, no default)
- [x] CLI: `run slurm` → `run --backend <name>`
- [x] CLI: `container build` → `build --backend <name>` (promoted to top-level)
- [x] CLI: `shell` command deleted (just SSH in manually)
- [x] CLI: `results` command deleted (tracking server handles this)
- [x] Deleted: `SSHClient`, `SlurmJobManager`, `ContainerBuilder`, `_quote_path` (old versions)
- [x] Deleted: `backend/slurm.py`, `backend/components/ssh.py`, `container/builder.py`
- [x] Deleted: `binds` config, `paths.bind()`, `BindNotFound` — use `cache_dir() / "name"` instead
- [x] `FileSyncer` updated: takes generic host (not SSHClient), uses `_quote_path` locally
- [x] `SlurmBackend` updated: no binds parameter, uses two-mount bind args (`/work` + `/cache`)

---

## Completed: Phase 5b — Backend Protocol + LocalBackend

Unified backend interface with a `Backend` protocol, `SweepSpec` dataclass, and `LocalBackend` implementation.

- [x] `Backend` protocol (`submit_sweep`, `list_jobs`, `cancel`, `cancel_all`, `get_status`, `wait_for_completion`)
- [x] `SweepSpec` dataclass — structured inputs replace raw bash command strings
- [x] `LocalBackend` — blocking, in-process `run_trial()` calls. All job management raises `UnsupportedOperation`. No config required.
- [x] `SlurmBackend.submit_sweep` refactored: accepts `SweepSpec`, command building moved from `cli.py` into `SlurmBackend`
- [x] CLI: `jernerics local` rewritten as thin wrapper → builds `SweepSpec` → `LocalBackend.submit_sweep`
- [x] CLI: `jernerics run` updated to use `SweepSpec` + refactored `SlurmBackend.submit_sweep`
- [x] CLI: removed `_build_setup_command`, `_build_trial_command` from `cli.py`
- [x] E2E verified: `LocalBackend` + gRPC tracking (all 6 event types: params, metrics, results, artifacts, sweep_meta, trial_end)
- [x] E2E verified: `SlurmBackend` dry-run produces identical script to pre-refactor

### Remaining (Phase 5c)
- [x] ~~Replace `FileSyncer` with rsync~~ — tar+scp is well-suited to WekaFS, code is now understood
- [x] ~~Implement `LocalSyncBackend`~~ — already done as `LocalBackend`
- [ ] Implement `LocalPueueBackend` and `BareBackend` (Pueue + Docker)
- [ ] `clean` command overhaul — safety check (all tracking synced, no jobs running). **Blocked by**: job resume needs a complete picture of what's safe to delete (zombie vs orphan .pb files, partial trial state). Do after resume lands.
- [ ] Integration test all CLI commands against real SLURM cluster

---

## Completed: Pass 2 — Auto-Retry System

Automatic retry of failed/stale trials in SLURM array sweeps. Handles three failure
modes: app crash (FAIL state), node death (stale RUNNING via heartbeat), and
pre-start failure (no trial created).

- [x] `retry.py` — `plan_retry()` computes stale/fresh/exhausted trial counts
- [x] `retry_checker.py` — checker job that runs after array, detects stale trials, submits retry chain
- [x] `RetryContext` — serialized context passed through chain (study name, config paths, depth)
- [x] `param_key()` — BLAKE2b hash of params so retry counts persist across trial numbers
- [x] Ledger tracks retry counts by param combo, not trial number — `max_retries` correctly enforced
- [x] Exhausted stale trials marked FAIL so they don't stay RUNNING forever
- [x] Heartbeat thread in runner — touches file every `heartbeat_interval_s`
- [x] `chain_depth_cap` safety limit on recursive checker submission
- [x] E2E tested: app crash (type 1), node death (type 2), persistent node death with retry exhaustion
- [x] Grid pre-enqueue via `enqueue_trial` — avoids GridSampler/BruteForceSampler TOCTOU race
- [x] Sentinel file for idempotent grid enqueue across concurrent array tasks
- [x] Sampler now passed to `create_study` (was silently ignored before)

### Config

```toml
[tool.jernerics.backends.hpc]
auto_retry = true
heartbeat_interval_s = 60
stale_after_s = 120
grace_period_s = 120
max_retries = 3
chain_depth_cap = 20
```

### Test configs (`examples/sweep-retry/`)

| Config | What it tests |
|--------|--------------|
| `config_app_crash` | Type 1: RuntimeError → FAIL, fresh trials submitted |
| `config_node_death` | Type 2: os._exit(9) → stale RUNNING, enqueued retry |
| `config_node_death_persistent` | Type 2: param-based crash → retry exhaustion, fresh trial |

## Completed: Phase 5c — Tooling

- [x] Create justfile with `lint`, `format`, `typecheck`, `test`, `check` recipes
- [x] Add `just` to Nix flake devShell
- [x] Fix ty type checking: set `VIRTUAL_ENV` in flake shellHook + `environment.extra-paths` in `[tool.ty]` config
- [x] Switch pre-commit hooks to local (system ruff/ty) so versions always agree
- [x] Add `BLE001`, `PLW1510` to ruff ignore; exclude `examples/` from ruff

---

## Completed: Server Entry Point

- [x] `jernerics_server/__main__.py` — `python -m jernerics_server [--db PATH] [--port PORT]` with signal handling
- [x] E2E verified: server → `LocalBackend` sweep → all event types in DuckDB

---

## Other backlog

- [ ] Task hooks (`on_task_start`, `on_task_complete`, `on_task_fail`)
- [ ] Explore structlog for structured logging
- [x] ~~`DAG.run()` unwrap decision~~ — returns `TaskResult` objects, indexable like dicts
- [x] ~~Delete `old_executor.py`~~ — already done
- [ ] Refactor DAG `resume()` — never used in production, needs review. Currently doesn't pass `tracker` to `execute_dag`.
- [ ] Write new integration tests reflecting real usage patterns
- [ ] Add MinIO artifact storage
- [ ] Build core marimo dashboard
- [ ] Server deploy: NixOS systemd service, health check, logging
- [ ] Server query API or marimo dashboard for inspecting DuckDB remotely
- [ ] the commands that have the --follow flag such as "jernerics logs --backend hpc 24200529 --follow" should automatically exit when a job finishes (currently they have to be manually killed with Ctrl+C)
- [ ] logs should be scoped per project currently they get put a directory that buckets all projects together, this makes cleaning up old logs difficult since you have to know which files belong to which project and makes manual inspection of logs more difficult since you have to open files to see which project they belong to. A better structure would be something like: `~/.cache/jernerics/<project_name>/logs/<job_id>.log` or potentually this even means that the cache dir should be `~/.cache/jernerics/<project_name>/` and then all the other files (optuna db, tracking pb files, etc) should also be scoped under the project name. This would make it much easier to manage multiple projects on the same machine and would make it easier to clean up old data when a project is finished since you could just delete the entire project directory. It would also make it easier to inspect logs since you could just look in the project directory for the logs instead of having to open files to see which project they belong to.
- [ ] SearchSweep is tightly coupled with the slurm currently since it has a slurm_overrides field. Instead it seems that it should be a more generic dict to pass overrides and then let each backend translate into the proper format for that backend.
