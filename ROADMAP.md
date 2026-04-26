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
- Write-local + rsync-eventually is more robust

### Design decisions (settled)
- **Writer**: JSONL per run (append-only, no lock contention, easy to rsync)
- **Data model**: `log_params(dict)`, `log_metric(key, value, step?)`, `log_result(key, value)` for arbitrary structured data
- **Visualization**: marimo notebooks querying the data store. Single core dashboard with project/experiment dropdowns, not per-project dashboards
- **Sync**: rsync from HPC
- **Optimization**: still single-scalar (via `objective` function). Multi-objective is a user decision expressed as an aggregate metric

### Plan
- [x] Design run directory structure and schema
- [x] Create `tracking/tracker.py` — `Tracker` class with `log_param()`, `log_metric()`, `log_result()`, `log_artifact()`
- [x] Integrate tracker into runner, DAG executor, and CLI
- [ ] Create `tracking/sync.py` — background sync thread to homelab server
- [ ] Build gRPC server with DuckDB backend
- [ ] Add MinIO artifact storage
- [ ] Build core marimo dashboard

---

## Phase 4 — Simplify HPC Layer

- [ ] Replace `FileSyncer` (tar/scp/extract) with rsync
- [ ] Unify cache directory logic — all runtime data (optuna, tracking) goes through `paths.cache_dir()`, eliminate `/work/.jernerics/` fallback and `use_scratch` branching. `cache_dir` config works everywhere, defaults to `~/.cache/jernerics/`

---

## Later / As Needed

- [ ] Task hooks (`on_task_start`, `on_task_complete`, `on_task_fail`)
- [ ] Explore Submitit for SLURM submission
- [ ] Explore structlog for structured logging
- [ ] Decide: should `DAG.run()` unwrap `TaskResult` to `dict[str, Any]` for ergonomics?
- [ ] Delete `old_executor.py`
- [ ] Refactor DAG `resume()` — never used in production, needs review. Currently doesn't pass `tracker` to `execute_dag`.
