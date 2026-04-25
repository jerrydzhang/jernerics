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

## Phase 2 — Extract the Runner Module

Replace `_get_sweep_runner_code()` f-string in `cli.py` with a real importable module.

### Why
- Double-brace escaping makes the ~120-line f-string unreadable
- Can't test directly — only runs inside a subprocess
- Error messages point to line numbers in a string that doesn't exist on disk
- Parameter passing is baked into the string

### Plan
- [ ] Create `src/jernerics/runner.py` (or `__main__.py` for `python -m jernerics.runner`)
- [ ] Move sweep trial logic from `_get_sweep_runner_code()` into a real function
- [ ] Parameters via CLI args or JSON manifest file (cli writes it, runner reads it)
- [ ] Update `run local` subprocess from `python -c <f-string>` to `python -m jernerics.runner`
- [ ] Update SLURM script generation similarly
- [ ] Write tests that import and test the runner directly
- [ ] Delete `_get_sweep_runner_code()`

### What the runner does
1. Load config file → `SweepConfig`
2. Build DAG from dag file
3. Create/load Optuna study
4. Trial loop: `study.ask()` → merge params with `_base` → `dag.run()` → extract objective → `study.tell()`
5. Handle failures: mark trial FAIL, report to Optuna

### Open decision
Strip MLflow before or after extraction? Extracting first preserves behavior; stripping first means the runner is born clean.

---

## Phase 3 — Replace MLflow with Simple Tracking

### Why
- MLflow is overkill for logging params + one metric per trial
- Optuna SQLite already stores params + objectives
- MLflow sync (local → remote) is fragile over unreliable WiFi
- Write-local + rsync-eventually is more robust

### Plan
- [ ] Create `tracking/writer.py` — `log_metric()` writes JSONL to run's state directory
- [ ] Create `tracking/sync.py` — background rsync to homelab
- [ ] Update runner to use new tracking instead of MLflow
- [ ] Delete: `MlflowConfig`, `mlflow sync` command, env vars in SLURM scripts, `mlflow_export_import` dependency
- [ ] Update `__init__.py` — replace `active_run_id` with `log_metric`

---

## Phase 4 — Simplify HPC Layer

- [ ] Replace `FileSyncer` (tar/scp/extract) with rsync

---

## Later / As Needed

- [ ] Task hooks (`on_task_start`, `on_task_complete`, `on_task_fail`)
- [ ] Explore Submitit for SLURM submission
- [ ] Explore structlog for structured logging
- [ ] Decide: should `DAG.run()` unwrap `TaskResult` to `dict[str, Any]` for ergonomics?
- [ ] Consider `_get_runner_code()` (single-run, non-sweep) — same f-string problem, smaller scope
- [ ] Delete `old_executor.py`
