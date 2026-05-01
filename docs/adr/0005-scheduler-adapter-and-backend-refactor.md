# Scheduler adapter and backend refactor

Replaces ADR 0001 and ADR 0002. The previous approach (shared orchestration functions + compositional components) reduced duplication but produced shallow interfaces — orchestration functions accepted callbacks that forced callers to understand the full flow, and script generation knowledge remained scattered across backends.

## Decision

Introduce a **SchedulerAdapter** protocol as the seam for scheduler-specific behavior. The **Backend** class becomes the orchestrator — it owns the deploy sequence, builds command strings, and delegates scheduling to the adapter. No separate orchestration module, no callbacks.

### Scheduler adapter

A per-scheduler component that owns script generation and job lifecycle for one scheduler type. Receives pre-wrapped command strings — does not see the container runtime or path resolver.

Interface:
- `submit_sweep(params: SweepSubmissionParams) -> SubmitResult` — composes setup + trials + post-hook using the scheduler's native mechanism
- `render_sweep(params: SweepSubmissionParams) -> str` — same as submit but returns the script (dry run)
- `submit_job(script, name, log_dir) -> str` — single job submission (builds)
- `list_jobs`, `cancel`, `cancel_all`, `get_status`, `wait_for_completion` — job lifecycle
- `get_logs(job_id, *, follow, stderr, meta)` — log retrieval, adapter resolves its own log path patterns
- `cleanup()` — scheduler-specific cleanup (e.g. `pueue clean`)

`SweepSubmissionParams` carries pre-wrapped command strings: `setup_command`, `trial_command`, `post_hook_command`, plus `n_trials`, `max_parallel`, `study_name`, `log_dir`, and a flat `overrides` dict interpreted by the adapter.

`SubmitResult` returns a `list[JobSubmission]` — each with `job_id`, `output_pattern`, `error_pattern`, `n_trials`. The backend saves all without interpreting.

### Backend as orchestrator

The Backend class composes: Host + ContainerRuntime + PathResolver + ProjectSync + SchedulerAdapter. Its methods build command strings (container wrapping, path resolution, cd, mkdirs, flock) and call the adapter. No callbacks — the deploy sequence is inline.

The Backend is deep: `prepare_and_submit` hides sync + readiness + retry context + command building + submission + meta saving in a single method.

### Post-hook

The "checker" is renamed to **post-hook** — a general post-sweep pipeline that currently implements retry and will expand to optuna sync and artifact upload. It is a single pre-wrapped command string passed to the adapter. The adapter internally composes it with the trials using its scheduler's native mechanism (Slurm: `--dependency=afterany`, Pueue: `pueue wait` wrapper).

### Command builder

A pure function (no host, no I/O) that takes spec info + container runtime + paths and returns the three command strings. Shared by the Backend and the post-hook (which needs to build command strings for retry resubmission).

### File organization

```
backend/
  adapter.py            → SchedulerAdapter protocol + SweepSubmissionParams
  backend.py            → Backend class (the orchestrator)
  factory.py            → make_backend(), make_adapter()
  models.py             → SubmitResult, JobSubmission, JobInfo, SweepSubmission
  local_backend.py      → LocalBackend (unchanged)
  host.py               → Host protocol, LocalHost, SSHHost, StdoutHost
  container.py          → ContainerRuntime protocol, Apptainer, Docker, NoContainer
  path_resolver.py      → PathResolver
  project_sync.py       → ProjectSync
  build_marker.py       → needs_rebuild, write_marker
  command_builders.py   → build_setup_command, build_trial_command, build_checker_command
  job_meta.py           → save_job_meta
  slurm/
    adapter.py           → SlurmAdapter
  pueue/
    adapter.py           → PueueAdapter
```

Top-level files are universal (used by every code path). `slurm/` and `pueue/` are conditional (loaded by factory based on config type). No `components/` subdirectory — flattened for clarity.

### Overrides

Flat dict interpreted by the adapter. Already scoped by `--backend` or `backend_overrides[backend_name]`. The adapter knows which keys it cares about and ignores the rest. No typed config translation layer.

## Rationale

### Why no callbacks

The previous orchestration functions (ADR 0002) sequenced steps but callers controlled each step via closures. The knowledge of "what happens during deploy" was split between the orchestration module (order) and the backend class (behavior). Neither side had the full picture. The Backend class already has all the dependencies as instance attributes — there's no need for a separate module to receive them as function arguments.

### Why pre-wrapped command strings

The adapter receives command strings that are already container-wrapped with resolved paths. This keeps the adapter's responsibility narrow: "given these runnable strings, schedule them on my scheduler." The alternative — letting the adapter do container wrapping — would require it to know about PathResolver and ContainerRuntime, duplicating the composition logic and making it harder to test.

### Why flat overrides instead of typed config

The overrides are already user-facing as a flat dict (`--set partition=priority`). A typed layer would translate dict → dataclass → dict. The adapter is the right place to interpret scheduler-specific keys. Adding a new scheduler means adding key handling in one place.

### Why SubmitResult is a list

Slurm produces multiple scheduler-level jobs (array + post-hook) with separate log paths. Pueue produces one group. A `list[JobSubmission]` normalizes this: the backend iterates and saves all, no conditional logic. This also fits the future generalization to job-level DAGs, where submissions are naturally a list.

### Why pueue wait instead of --after for post-hook

Pueue's `--after` dependency only fires when upstream tasks succeed. The post-hook must run regardless of trial outcomes (it needs to detect and retry failures). `pueue wait` blocks a worker slot but correctly runs after both successes and failures.

## Consequences

- Adding a new scheduler (PBS, LSF) means creating a new `backend/<scheduler>/adapter.py` that implements the SchedulerAdapter protocol. No changes to Backend, CLI, or other adapters.
- The post-hook is a Python entry point that can grow a pipeline of operations (retry → optuna sync → artifact upload) without changing the adapter interface.
- The retry checker calls `make_adapter()` instead of `make_backend()` — it needs the scheduler adapter for resubmission but not the full deploy pipeline.
- ADR 0001 (shared backend logic via composition) is superseded — the Backend class now owns the shared logic directly.
- ADR 0002 (pure container runtimes and shared orchestration functions) is superseded — the adapter replaces the orchestration module, and pure container runtimes are unchanged.
