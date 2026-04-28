# Jernerics Roadmap

Living document. Updated as decisions are made and phases complete.

## Project Direction

Transitioning from a vibe-coded prototype to a tool where every line is understood. The work is a ground-up rewrite of core subsystems — fixing bugs, eliminating LLM-generated code, and replacing heavyweight dependencies with simple, inspectable alternatives.

Long-term: a lean experiment runner for HPC that does DAG scheduling, sweep optimization, and result tracking without the overhead.

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

## Phase 5b — Multi-Backend Implementation

### Remaining
- [ ] Replace `FileSyncer` (tar/scp/extract) with rsync
- [ ] Implement `LocalSyncBackend` (blocking loop) — unifies with `run local`
- [ ] Implement `LocalPueueBackend` and `BareBackend` (Pueue + Docker)
- [ ] `clean` command overhaul — safety check (all tracking synced, no jobs running)
- [ ] Integration test all CLI commands against real SLURM cluster

---

## Completed: Phase 5c — Tooling

- [x] Create justfile with `lint`, `format`, `typecheck`, `test`, `check` recipes
- [x] Add `just` to Nix flake devShell
- [x] Fix ty type checking: set `VIRTUAL_ENV` in flake shellHook + `environment.extra-paths` in `[tool.ty]` config
- [x] Switch pre-commit hooks to local (system ruff/ty) so versions always agree
- [x] Add `BLE001`, `PLW1510` to ruff ignore; exclude `examples/` from ruff

---

## Other backlog

- [ ] Task hooks (`on_task_start`, `on_task_complete`, `on_task_fail`)
- [ ] Explore structlog for structured logging
- [ ] Decide: should `DAG.run()` unwrap `TaskResult` to `dict[str, Any]` for ergonomics?
- [ ] Delete `old_executor.py`
- [ ] Refactor DAG `resume()` — never used in production, needs review. Currently doesn't pass `tracker` to `execute_dag`.
- [ ] Write new integration tests reflecting real usage patterns
- [ ] Add MinIO artifact storage
- [ ] Build core marimo dashboard
