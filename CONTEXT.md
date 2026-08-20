# Jernerics

A toolkit for running hyperparameter sweeps over ML experiments across multiple execution backends (local, Slurm, Pueue). Each experiment is authored as a plain `trial(config, tracker)` function.

## Language

**Sweep**:
A single hyperparameter search — defines a search space, a number of trials, and an objective. One sweep maps to one Optuna study.
_Avoid_: study (Optuna-internal term, not user-facing)

**Trial**:
A single run within a sweep. One invocation of the user's `trial(config, tracker)` function with a specific parameter combination drawn from the search space. Identified by a UUID and a number; a retried trial points at its root so a family reads as generations.

**Backend**:
A scheduler-backed execution environment that runs sweeps. Slurm and Pueue are the two backend types. Composed from an Orchestrator, a Scheduler Adapter, and a ProjectSync.

**Local runner**:
The in-process trial runner invoked by `jernerics local`. Not a backend — has no host, no container, no scheduler. Used for debugging and iteration.
_Avoid_: local backend (it doesn't implement the Backend protocol)

**Deploy**:
The full remote submission sequence: sync project source → build container → submit sweep. Triggered by `jernerics run`.
_Avoid_: prepare_and_submit (implementation name), stage, launch

**Job**:
A single unit of work submitted to a scheduler backend. One sweep may produce multiple jobs (e.g. the array job + checker job on Slurm). Meaningful only for scheduler backends — Local runs directly.
_Avoid_: trial (that's the user's function, not a scheduler unit)

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

**Event**:
A single tracked record in the v3 wire contract — sweep/submission/job snapshots, trial snapshots, execution lifecycle (start, heartbeat, progress, end), manual params, values, artifact declarations. One JSON object per line in the local JSONL buffer; shipped in batches to the server's `/ingest`. Ingest is idempotent per event id, so live shipping, replay, and reconciliation overlapping is safe.
_Avoid_: envelope (v2 term)

**Stream**:
The live path: the runner's tracker appends each event to a local `.jsonl` file, and a ship client tails it from a durable byte cursor and POSTs batches to the server's `/ingest` endpoint while the trial is running — so metrics appear live.
_Avoid_: sync (ambiguous with the CLI command / replay path)

**Replay**:
The batch path: after trials finish, any `.jsonl` events not fully shipped live are replayed to the server via HTTP POST. Triggered by `jernerics tracking replay` and the post-hook. Ingest is idempotent per event id, so live + replay overlapping is safe.

**Query endpoint**:
An HTTP endpoint (`POST /query`) on the tracking server that accepts read-only SQL and returns JSON rows. The expert escape hatch for questions the typed domain reads cannot answer; everything routine goes through the domain endpoints.

**Execution**:
One attempt to run a trial — identified by a UUID, owned by the trial. Heartbeats, explicit progress (`current`/`total`/`unit`), stored stdout/stderr, and artifacts attach to executions. Monitoring labels (active / quiet / stale / ended) are derived on read from the last heartbeat and the outcome, never stored.
_Avoid_: run (v2 term — the v3 store has no run entity)

**Submission**:
One deployment of a sweep to a backend: which backend, scheduler state, expected trials, git hash, and config source. Owns its scheduler jobs. Emitted at deploy time by the deploy path.

**Selection**:
The typed scope every domain read is pinned to: a project plus optional sweep/trial/retry-root/execution ids. Encodable as an opaque token (`encode_selection`) so a dashboard URL can hand the exact same scope to a notebook or script.

**Observability CLI**:
The read surface over tracked data: `runs` (list trials with derived monitoring), `summary` (one trial's lineage, params, values, artifacts, executions), `diff` (compare two trials), `trace` (one value key's step series), `query` (raw SQL escape hatch), plus `replay`. All commands support `--json` for agent consumption.

**Trace**:
The raw `[step, value]` series for a single value key on one trial — scalar values as floats, JSON observations as canonical JSON text. Surfaced without interpretation; the CLI does not summarize or visualize it. For humans: a step-by-step listing. For agents: `--json` returns the full series for reasoning.

**Scalar value**:
A tracked value with a float payload, logged over steps (loss, lr, grad_norm, token_acc, …) with an optional flat scalar context (e.g. `{"phase": "train"}`) that becomes a queryable dimension.

**JSON observation**:
A tracked value carrying an arbitrary JSON object payload, bounded to 64 KiB encoded — anything larger belongs in an artifact, not a value.

**Dashboard**:
The read-only web UI mounted on the tracking server (`/dashboard/...`): live operational monitoring, sweep/trial/execution pages, artifact and stored-log viewers, cross-sweep analysis, and a continue-in-Python selection handoff. Browser login exchanges the API key for a signed session cookie. The CLI handles quick check-ins; the dashboard handles the rest.

**Tracking server**:
A single HTTP process. Ingests tagged events (`POST /ingest`), serves typed domain reads (`/sweeps`, `/trials`, `/values`, ...), raw SQL (`POST /query`), and stores/serves immutable artifact blobs (`PUT`/`GET /artifact/{id}`) on its own disk. Owns the SQLite file exclusively. Authenticated via a bearer API key in the `Authorization` header. No external object storage — artifacts live on the server's disk.

**Funnel vs tailnet**:
All HTTP traffic (ingestion from HPC, replay from post-hook, live streaming from local runs) goes through the Tailscale funnel URL with TLS. The read and artifact endpoints listen on the same process — tailnet-only, accessible only from personal devices. API key auth applies everywhere regardless.

**Heartbeat**:
A file touched periodically by a running trial, mirrored as an `execution_heartbeat` event so the server sees liveness live. Exists under `heartbeats/<trial_number>.heartbeat`. Used to detect stale (presumably dead) executions; the derived label is computed on read, never stored.

**Retry**:
The system that detects stale trials (via heartbeats), marks them failed in Optuna, and resubmits them carrying lineage (`retry_of`, `retry_root`, `retry_index`) so retries render as families/generations. Composed of a retry plan, a retry context, and a checker job.
_Avoid_: auto-retry (that's the config flag name, not the concept)

**Scheduler adapter**:
A per-scheduler component that knows how to format and submit jobs to one scheduler type (Slurm, Pueue). Owns script generation and job lifecycle (list, cancel, status, wait). Interprets scheduler-specific overrides. Receives pre-wrapped command strings — does not see the container runtime or path resolver.
_Avoid_: backend (that's the composed system, not just the scheduler part)

**Orchestrator**:
The shared layer that composes host + container runtime + path resolver + project syncer + scheduler adapter. Owns the deploy sequence: sync project → check readiness → build command strings → submit via adapter → save meta. Builds the three command strings (setup, trial, post-hook) that the adapter receives.
_Avoid_: orchestration (that's the module name, not the concept)

**Post-hook**:
The process that runs on the remote after all trials finish. Performs retry detection/resubmission, reconciles the optuna journal with terminal server state, replays tracking events, and uploads pending artifact blobs to the server.
_Avoid_: checker (that's the current implementation name — it will grow beyond checking)

## Relationships

- **PathResolver** is the single source of truth for path resolution. All orchestration code (build, submit, retry) uses it to get `work_prefix`, `cache_prefix`, and `storage_path`. No `isinstance(container, NoContainer)` checks in orchestration code.
- A **Scheduler Adapter** receives pre-wrapped command strings (setup, trial, post-hook) and decides how to compose them on its scheduler (e.g. Slurm uses `--dependency`, Pueue uses inline `wait`).
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
