# Jernerics

A toolkit for running hyperparameter sweeps over DAG-structured ML experiments across multiple execution backends (local, Slurm, Pueue).

## Language

**Sweep**:
A single hyperparameter search — defines a search space, a number of trials, and an objective. One sweep maps to one Optuna study.
_Avoid_: study (Optuna-internal term, not user-facing)

**Trial**:
A single run within a sweep. Executes the full DAG with one parameter combination drawn from the search space.

**Task**:
A node in a DAG — a Python function decorated with `@task`, with declared dependencies on other tasks.

**DAG**:
A directed acyclic graph of tasks, defined in a Python file. The unit of work executed per trial.

**Backend**:
A scheduler-backed execution environment that runs sweeps. Slurm and Pueue are the two backend types. Composed from an Orchestrator, a Scheduler Adapter, and a ProjectSync.
_Avoid_: executor, runner (those refer to the DAG executor, not the backend)

**Local runner**:
The in-process trial runner invoked by `jernerics local`. Not a backend — has no host, no container, no scheduler. Used for debugging and iteration.
_Avoid_: local backend (it doesn't implement the Backend protocol)

**Deploy**:
The full remote submission sequence: sync project source → build container → submit sweep. Triggered by `jernerics run`.
_Avoid_: prepare_and_submit (implementation name), stage, launch

**Job**:
A single unit of work submitted to a scheduler backend. One sweep may produce multiple jobs (e.g. the array job + checker job on Slurm). Meaningful only for scheduler backends — Local runs directly.
_Avoid_: task (that's a DAG node)

**Project**:
The user's codebase being experimented on. Identified by the nearest `pyproject.toml` with a `[tool.jernerics]` section.

**Host path**:
A filesystem path as seen by the host machine. What SSH, scp, and sbatch scripts operate on.
_Avoid_: path (ambiguous — host or container?), remote path, local path

**Container path**:
A filesystem path as seen inside the container. `/work` maps to project source, `/cache` to ephemeral data.
_Avoid_: path (ambiguous — host or container?)

**Sweep config**:
A Python file defining `base`, `search_space`, `n_trials`, `objective`, etc. Describes *what* to sweep. Loaded via `runpy`.
_Avoid_: config (ambiguous — could be backend config), experiment config

**Backend config**:
A named profile in `[tool.jernerics.backends.<name>]` inside `pyproject.toml`. Describes *where and how* to execute. Loaded by `load_backend_config(name)`.
_Avoid_: config (ambiguous — could be sweep config)

**Container starter**:
A minimal container definition file (`.def`) that `jernerics init` copies into a new project. Not a template — no variable substitution, just a starting point.
_Avoid_: template (implies variable substitution)

**Tracker**:
The interface (`Tracker` protocol) for logging params, metrics, results, and artifacts during a trial.

**Envelope**:
A single tracked event (param, metric, result, artifact, or trial_end). Serialized as delimited protobuf.

**Stream**:
The real-time path: ProtobufTracker writes envelopes to a local `.pb` file, and a streaming client forwards them to the gRPC server while the trial is running.
_Avoid_: sync (ambiguous with the CLI command / replay path)

**Replay**:
The batch path: after trials finish, orphaned `.pb` files on the remote are replayed to the gRPC server via SSH. Triggered by the `sync` CLI command.

**Query endpoint**:
An HTTP endpoint on the tracking server that accepts SQL and returns JSON rows. The thin interface between analytical clients (dashboard, notebooks) and the DuckDB store. Transport is swappable (future: Arrow Flight) as long as the interface stays stateless.

**Dashboard**:
A React SPA served as static files by the tracking server. Renders sweep comparison views using ECharts with a base16-derived theme. Shows artifact previews inline (images, tables) with download links for other types. Artifacts are displayed in trial/sweep context — not a separate browser. Lives in a separate repo (`jernerics-dashboard`) and is wired into the NixOS module via a nullable `dashboardPackage` option.

**Tracking server**:
A single process running gRPC ingestion, HTTP query endpoint, and static file serving for the dashboard. Owns the DuckDB file exclusively — all reads go through the HTTP query endpoint. Authenticated via API key in gRPC metadata / HTTP header. Proxies artifact downloads from MinIO — single URL, single auth point.

**Funnel vs tailnet**:
All gRPC traffic (ingestion from HPC, replay from post-hook, streaming from local runs) goes through the Tailscale funnel URL with TLS. The dashboard, HTTP query endpoint, and artifact proxy listen on a separate port not exposed via funnel — tailnet-only, accessible only from personal devices. API key auth applies everywhere regardless.

**Heartbeat**:
A file touched periodically by a running trial. Used to detect stale (presumably dead) trials. Exists under `heartbeats/<trial_number>.heartbeat`.

**Retry**:
The system that detects stale trials (via heartbeats), marks them failed in Optuna, and resubmits them. Composed of a retry plan, a retry context, and a checker job.
_Avoid_: auto-retry (that's the config flag name, not the concept)

**Scheduler adapter**:
A per-scheduler component that knows how to format and submit jobs to one scheduler type (Slurm, Pueue). Owns script generation and job lifecycle (list, cancel, status, wait). Interprets scheduler-specific overrides. Receives pre-wrapped command strings — does not see the container runtime or path resolver.
_Avoid_: backend (that's the composed system, not just the scheduler part)

**Orchestrator**:
The shared layer that composes host + container runtime + path resolver + project syncer + scheduler adapter. Owns the deploy sequence: sync project → check readiness → build command strings → submit via adapter → save meta. Builds the three command strings (setup, trial, post-hook) that the adapter receives.
_Avoid_: orchestration (that's the module name, not the concept)

**Post-hook**:
The process that runs on the remote after all trials finish. Currently performs retry detection and resubmission. Will expand to include optuna sync and artifact upload. Generalizing the checker into an extensible post-sweep pipeline.
_Avoid_: checker (that's the current implementation name — it will grow beyond checking)

## Relationships

- **PathResolver** is the single source of truth for path resolution. All orchestration code (build, submit, retry) uses it to get `work_prefix`, `cache_prefix`, and `storage_path`. No `isinstance(container, NoContainer)` checks in orchestration code.

- A **Project** has many **Sweeps**.
- A **Sweep** has many **Trials**.
- Each **Trial** executes a **DAG** with a specific parameter combination.
- A **DAG** contains **Tasks** with declared dependencies.
- A **DAG Runner** executes tasks within a DAG (sync or thread-pool).
- The **Runner script** (`runner.py`) is invoked inside the execution environment to run a single trial — it loads the Optuna study, executes the DAG, and handles tracking.
- An **Orchestrator** composes the deploy sequence. It builds command strings and delegates scheduling to a **Scheduler Adapter**.
- A **Scheduler Adapter** receives pre-wrapped command strings (setup, trial, post-hook) and decides how to compose them on its scheduler (e.g. Slurm uses `--dependency`, Pueue uses inline `wait`).
- A **Deploy** (sync → build → submit) sends a **Sweep** to a **Backend** via the **Orchestrator**.
- A **Post-hook** runs after all trials finish. Currently implements retry. Will expand to optuna sync and artifact upload.
- Heartbeat and retry are separate subsystems — heartbeat detects staleness, retry acts on it — but they belong to the same domain (auto-retry).

**Container runtime**:
A pure command factory — produces shell commands for building, checking, and wrapping container execution. Holds no host reference, performs no side effects. The host executes the commands.
_Avoid_: container (ambiguous — could mean the running container instance)

**Job submission**:
The scheduler adapter's `submit_job` method produces a bash script fragment that submits one command to the scheduler and captures its ID. Sweep submission (array + post-hook composition) is adapter-specific.

## Flagged ambiguities

- "study" was used to mean both **Sweep** (the user concept) and the Optuna Study object — resolved: **Sweep** is the canonical term; "study" is Optuna-internal.
- "sync" was used for both the real-time streaming path (`FileSyncClient`) and the batch replay path (`sync` CLI command) — resolved: real-time is **stream**, batch is **replay**.
- `FileSyncClient` (tracking stream) and `FileSyncer` (project source sync) have confusingly similar names — resolved: `FileSyncClient` → **stream** vocabulary, `FileSyncer` → `ProjectSync`.
- `SweepSpec` → `SweepSubmission`: the resolved, runtime-ready object passed to backends for submission. Distinguishes `SweepConfig` (user-authored) from `SweepSubmission` (system-resolved).
- "path" is ambiguous between **host path** and **container path** — always use the full compound term when context doesn't make it obvious.
- "local backend" was used for both the in-process debugger and a potential local pueue backend — resolved: **local runner** for the in-process thing, **local pueue** for the scheduler-backed local execution.
- "container template" implies variable substitution — resolved: **container starter** is a minimal definition file copied into a new project.
- "checker" was the name for the post-sweep process — resolved: **post-hook** is the canonical concept; "checker" is the current retry-only implementation.
- "backend" was used for both the composed system and the per-scheduler component — resolved: **backend** is the composed system; **scheduler adapter** is the per-scheduler component.
