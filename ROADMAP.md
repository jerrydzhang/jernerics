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

## Phase 3b — Custom Tracking

Replace MLflow with a lightweight tracking layer + marimo dashboard.

### Why
- MLflow is overkill for logging params + one metric per trial
- MLflow sync (local → remote) is fragile over unreliable WiFi
- Need support for structured results (e.g., Pareto frontiers), not just flat scalars
- Want project-level grouping and log-scale plots — MLflow doesn't provide these well
- Write-local + sync-eventually is more robust

### Design decisions (settled)
- **Schema + local format**: Protobuf (varint-length-prefixed delimited)
- **Transport**: gRPC — both client and server use generated gRPC stubs. Client sends events, server receives and stores.
- **Remote metrics store**: DuckDB (server-side only)
- **Remote artifact store**: MinIO (not yet built)
- **Visualization**: marimo notebooks querying DuckDB. Single core dashboard with project/experiment dropdowns, not per-project dashboards
- **Optimization**: still single-scalar (via `objective` function). Multi-objective is a user decision expressed as an aggregate metric

### Monorepo structure
Three packages in a uv workspace:
- `packages/jernerics-proto/` — proto schema + generated pb2/grpc files. Both other packages depend on this.
- `packages/jernerics/` — client library (DAG executor, HPC, CLI, ProtobufTracker). Depends on `jernerics-proto`.
- `packages/jernerics-server/` — gRPC service, DuckDB store, dashboard. Depends on `jernerics-proto`, `grpcio`, `duckdb`.

This keeps HPC installs slim (no DuckDB, no server code) and the server package isolated.

### Plan
- [x] Design run directory structure and schema
- [x] Create `tracking/tracker.py` — `Tracker` class with `log_param()`, `log_metric()`, `log_result()`, `log_artifact()`
- [x] Integrate tracker into runner, DAG executor, and CLI
- [x] Monorepo refactor — split into three packages with uv workspace
- [x] Build gRPC server with DuckDB backend
- [x] Create sync client (`client.py`) — background thread sends events to server via gRPC
- [x] `jernerics sync` CLI command — replay orphaned .pb files to server
- [ ] Add MinIO artifact storage
- [ ] Build core marimo dashboard

---

## Completed: Phase 4 — Cache Paths + Sync

- [x] Add `paths.cache_dir()` — resolves to `~/.cache/jernerics/<project>` locally, `hpc_config.cache_dir` on HPC
- [x] Eliminate `use_scratch` branching in `_generate_sweep_script()` — container always sees `/work` + `/cache`
- [x] Update `run_local` to use `cache_dir()` for optuna and tracking directories
- [x] Add `jernerics sync` command — replays .pb files to tracking server via apptainer exec on HPC
- [x] Add `tracking/data_sync.py` — concurrent replay with retry logic
- [x] Add `tracking/replay_runner.py` — `__main__` entry point invoked on HPC
- [x] Rename `hpc/sync.py` → `hpc/project_sync.py` for clarity

### Remaining
- [ ] Replace `FileSyncer` (tar/scp/extract) with rsync
- [ ] `clean` command overhaul — should clean all cache contents, not just logs

## Phase 5 — Multi-Backend Remote Execution

Support running on bare metal workstations in addition to SLURM HPC.

### Design Notes
- Current SLURM coupling is concentrated in: `SlurmJobManager`, `_generate_sweep_script()`, `_get_hpc_client()`, `shell` command (`srun`)
- Commands that are SSH + file ops only (no SLURM): `logs`, `results`, `clean`, `sync` — these need minimal changes
- Commands deeply SLURM-coupled: `run slurm`, `container build`, `jobs`, `cancel`, `shell`
- Natural abstraction: a `Remote` protocol with `SLURMRemote` and `BareMetalRemote` implementations
- The path layout is already backend-agnostic (`/work` + `/cache` inside container)
- The runner is already decoupled from SLURM (just `python -m jernerics.runner`)

### Plan
- [ ] Design `Remote` protocol abstraction
- [ ] Implement `BareMetalRemote` (SSH + tmux/nohup for job management)
- [ ] Refactor CLI commands to use `Remote` instead of direct SLURM calls
- [ ] Add remote type to config (`[tool.jernerics.hpc] type = "slurm" | "bare"`)

---

- [ ] Task hooks (`on_task_start`, `on_task_complete`, `on_task_fail`)
- [ ] Explore Submitit for SLURM submission
- [ ] Explore structlog for structured logging
- [ ] Decide: should `DAG.run()` unwrap `TaskResult` to `dict[str, Any]` for ergonomics?
- [ ] Delete `old_executor.py`
- [ ] Refactor DAG `resume()` — never used in production, needs review. Currently doesn't pass `tracker` to `execute_dag`.
- [x] Switch to uv2nix for Nix packaging (needed for server deployment to homelab)
- [ ] Write new integration tests reflecting real usage patterns
