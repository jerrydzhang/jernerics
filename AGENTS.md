# AGENTS.md

## Project Documentation

- **`docs/vision.md`** — Why the project exists, who it's for, principles, non-goals, and direction. Read this first; it is the anchor for every design decision and the test for whether new work belongs here at all.
- **`CONTEXT.md`** — Domain glossary. Read this before working on the project. It defines canonical terms (sweep, trial, task, backend, deploy, etc.) and flags ambiguities. When you use a term, check that it matches the glossary.

## Environment

The project uses **uv2nix** (not a local `.venv`). All Python packages live in the Nix store. The devShell sets:

- `VIRTUAL_ENV` → points to the nix-built virtualenv
- `LD_LIBRARY_PATH` → includes `stdenv.cc.cc.lib` for native extensions (numpy)
- `PYTHONPATH` → unset (editable overlay uses `$REPO_ROOT`)

**Never create or use a `.venv` directory.** If `uv run` creates one, delete it. All commands should use the nix shell environment directly.

**Never use `uv run` or `uv sync` to execute code or install packages.** These commands create or pick up a stale local `.venv` that shadows the nix store packages, causing import errors and test failures. Use `python3`, `pytest`, or `just` recipes instead — the devShell already has everything installed.

## Commands

All commands run from repo root inside the nix devShell (`nix develop`).

```bash
# Test
just test                                       # All tests
just test-unit                                  # Unit tests only
pytest packages/jernerics/tests/unit/tracking/test_tracker.py  # Specific file
pytest packages/jernerics/tests/unit/tracking/test_tracker.py::TestLogParam::test_float  # Single test
pytest -x                                       # Stop on first failure

# Lint & format (must pass before committing)
just lint                                       # Lint
just lint-fix                                   # Auto-fix lint issues
just format                                     # Format
just format-check                               # Check formatting without changes

# Type check
just typecheck

# All checks at once
just check
```

## Project Structure

uv workspace monorepo at repo root:

```
packages/
  jernerics/             # Client library (backend, CLI, tracking, runner)
  jernerics-server/      # HTTP tracking server (SQLite store, /query, /ingest, /artifact)
```

Source layout inside `packages/jernerics/`:

```
src/jernerics/
  cli.py                 # Typer CLI — all commands
  config.py              # BackendConfig, SweepConfig, config loading
  runner.py              # Trial runner invoked via python -m jernerics.runner
  paths.py               # cache_dir(), work(), is_hpc()
  post_hook.py           # Post-sweep hook (replay tracking, sync to server)
  retry.py               # Retry orchestration logic
  retry_checker.py       # Heartbeat staleness detection
  backend/               # Multi-backend execution
    adapter.py           # SchedulerAdapter protocol + SweepSubmissionParams
    backend.py           # Backend class (orchestrator: host + container + adapter)
    factory.py           # make_backend(), make_adapter()
    models.py            # SweepSubmission, SubmitResult, JobSubmission, JobInfo
    command_builders.py  # build_sweep_commands, build_setup/trial/checker_command
    host.py              # Host protocol, LocalHost, SSHHost, StdoutHost
    container.py         # ContainerRuntime protocol, Apptainer, Docker, NoContainer
    path_resolver.py     # PathResolver
    project_sync.py      # ProjectSync (tar/scp project sync)
    job_meta.py          # save_job_meta
    build_marker.py      # needs_rebuild, write_marker
    local_backend.py     # LocalBackend (blocking, in-process)
    slurm/               # Slurm scheduler adapter
      adapter.py         # SlurmAdapter (sbatch + Apptainer)
    pueue/               # Pueue scheduler adapter
      adapter.py         # PueueAdapter (pueue + Docker/Apptainer/none)
  container/
    templates.py         # Container definition templates (.def and Dockerfile)
  tracking/              # JSONL tracker, HTTP ship client, replay, artifact upload/manifest
                         # Also: infra, trial_environment, envelope (TypedDicts)
tests/
  unit/                  # Mirrors src/ structure
```

Run commands inside the nix devShell. The `just` recipes wrap `uv run` internally, but direct `pytest` invocations also work since `VIRTUAL_ENV` is set by the shell hook. Never create a `.venv`.

## Definition of Done

A task is complete when ALL of the following pass:

1. `just lint` exits 0
2. `just format-check` exits 0
3. `just test` exits 0 with no failures
4. Changed files have been staged

## Code Conventions

### What ruff already enforces (don't repeat in code)

- Line length 88, double quotes, 4-space indent, trailing commas in multi-line
- Import ordering (stdlib → third-party → local)
- Unused imports, unreachable code, etc.

### What ruff does NOT enforce

- **No comments on self-documenting code.** If you feel the need to comment, consider renaming first.
- **Use `Self` from `typing`** for `__enter__` return types, not the class name directly.
- **No `from __future__ import annotations`.** Removed project-wide — not needed for Python 3.12+. Use string annotations (`"Task"`) for forward references and `Self` from `typing` for self-referencing class methods.
- **Use `@dataclass`** for data-holding classes. Use `field(default_factory=...)` for mutable defaults.
- **Define custom exceptions** at module level with descriptive names (`ConfigNotFound`, not `Error`).
- **Use `check=False`** on `subprocess.run` calls where you inspect `returncode` yourself.

### Tilde expansion

`~` only expands by the shell in specific contexts. Get this wrong and paths become literals.

- **SSH commands** (via SSHHost passthrough args): `~` expands. Use it directly: `host.run(["mkdir", "-p", "~/foo"])`
- **SLURM directives, quoted strings in scripts**: `~` does NOT expand. Use `$HOME`: `remote_dir.replace("~", "$HOME")`

### Container-aware code

The container sees `/work` (project source) and `/cache` (ephemeral data). Never hardcode host paths in generated scripts. Use `paths.cache_dir()` for ephemeral storage — no custom bind mounts.

### Config layers

1. **`pyproject.toml` `[tool.jernerics.*]`** — infrastructure (named backends, tracking server). Loaded by `load_backend_config(name)` and `load_tracking_server()`.
2. **User's experiment config** (Python file with `base`, `search_space`, `n_trials`, etc.). Loaded by `load_config()` via `runpy.run_path()`.

## Pre-commit and Commits

**Never use `--no-verify` when committing.** This is non-negotiable. The previous commit passed all checks, so if pre-commit fails, something in the current changes broke it — fix it, don't skip it.

**Never claim errors are "pre-existing" without verifying.** This is the most common failure mode. When lint, type checks, or tests fail, the agent reflexively labels them pre-existing to justify skipping. The previous commit passed — if something fails now, your changes caused it. Verify with `git stash && just lint` if genuinely uncertain.

## When Blocked

- If tests fail with `libstdc++.so.6: cannot open shared object file`: you are not in the nix devShell. Run `nix develop` first.
- If tests fail after 3 attempts: stop and report the failing test with full output
- If a dependency is missing: check `pyproject.toml` first, then ask
- If you encounter an import error: verify the file exists and the module name matches — recent renames may not be in your training data
- **Never:** delete files to resolve errors, skip tests, modify test configuration files, or add `# type: ignore` / `# noqa` without justification
- **Never:** create a `.venv`. If one exists, delete it — the nix store has the correct packages.

## Decision Points

When implementing a scoped task, **stop and report back** if you encounter:

- A non-trivial design decision (multiple reasonable approaches)
- An ambiguity in the spec that affects behavior
- A failure you can't resolve in one attempt

Do not resolve these on your own. Explain the situation and wait for direction. Continuing past a decision point without consulting the user is worse than stopping early.

## Type Checking

When `just typecheck` (or `ty`) reports errors, fix the underlying type issues. **Never exclude entire packages or directories from type checking to suppress errors.** If a dependency can't be resolved (e.g., duckdb only in jernerics-server's venv), use targeted `# ty: ignore[unresolved-import]` on specific import lines — not broad exclusions.

## Packages

The `jernerics-proto` package and gRPC/protobuf transport were removed. Tracking is now JSONL over HTTP (see `packages/jernerics/src/jernerics/tracking/envelope.py` for the envelope shape). The `just proto` recipe is gone.

<!-- br-agent-instructions-v1 -->

---

## Beads Workflow Integration

This project uses [beads_rust](https://github.com/Dicklesworthstone/beads_rust) (`br`/`bd`) for issue tracking. Issues are stored in `.beads/` and tracked in git.

### Essential Commands

```bash
# View ready issues (open, unblocked, not deferred)
br ready              # or: bd ready

# List and search
br list --status=open # All open issues
br show <id>          # Full issue details with dependencies
br search "keyword"   # Full-text search

# Create and update
br create --title="..." --description="..." --type=task --priority=2
br update <id> --status=in_progress
br close <id> --reason="Completed"
br close <id1> <id2>  # Close multiple issues at once

# Sync with git
br sync --flush-only  # Export DB to JSONL
br sync --status      # Check sync status
```

### Workflow Pattern

1. **Start**: Run `br ready` to find actionable work
2. **Claim**: Use `br update <id> --status=in_progress`
3. **Work**: Implement the task
4. **Complete**: Use `br close <id>`
5. **Sync**: Always run `br sync --flush-only` at session end

### Key Concepts

- **Dependencies**: Issues can block other issues. `br ready` shows only open, unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers 0-4, not words)
- **Types**: task, bug, feature, epic, chore, docs, question
- **Blocking**: `br dep add <issue> <depends-on>` to add dependencies

### Session Protocol

**Before ending any session, run this checklist:**

```bash
git status              # Check what changed
git add <files>         # Stage code changes
br sync --flush-only    # Export beads changes to JSONL
git commit -m "..."     # Commit everything
git push                # Push to remote
```

### Best Practices

- Check `br ready` at session start to find available work
- Update status as you work (in_progress → closed)
- Create new issues with `br create` when you discover tasks
- Use descriptive titles and set appropriate priority/type
- Always sync before ending session

<!-- end-br-agent-instructions -->

<!-- bv-agent-instructions-v3 -->

---

## Beads Workflow Integration

This project uses [beads_rust](https://github.com/Dicklesworthstone/beads_rust) (`br`) for issue tracking and [beads_viewer](https://github.com/Dicklesworthstone/beads_viewer) (`bv`) for graph-aware triage. Issues are stored in `.beads/` and tracked in git. Current `br` workspaces normally export `.beads/issues.jsonl`; older `bd`/legacy workspaces may use `.beads/beads.jsonl`. `bv` auto-discovers the supported JSONL files, so agents should use `br`/`bv` commands instead of hard-coding a single filename.

### Using bv as an AI sidecar

bv is a graph-aware triage engine for Beads projects. Instead of parsing .beads/issues.jsonl / .beads/beads.jsonl directly or hallucinating graph traversal, use robot flags for deterministic, dependency-aware outputs with precomputed metrics (PageRank, betweenness, critical path, cycles, HITS, eigenvector, k-core).

**Scope boundary:** bv handles *what to work on* (triage, priority, planning). `br` handles creating, modifying, and closing beads.

**CRITICAL: Use ONLY --robot-* flags. Bare bv launches an interactive TUI that blocks your session.**

#### The Workflow: Start With Triage

**`bv --robot-triage` is your single entry point.** It returns everything you need in one call:
- `quick_ref`: at-a-glance counts + top 3 picks
- `recommendations`: ranked actionable items with scores, reasons, unblock info
- `quick_wins`: low-effort high-impact items
- `blockers_to_clear`: items that unblock the most downstream work
- `project_health`: status/type/priority distributions, graph metrics
- `commands`: copy-paste shell commands for next steps

```bash
bv --robot-triage        # THE MEGA-COMMAND: start here
bv --robot-next          # Minimal: just the single top pick + claim command

# Token-optimized output (TOON) for lower LLM context usage:
bv --robot-triage --format toon
```

Before claiming, verify current state with `br show <id> --json` or `br ready --json`. `recommendations` can include graph-important blocked or assigned work; only `quick_ref.top_picks` and non-empty `claim_command` fields represent claimable work.

#### Other bv Commands

| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with unblocks lists |
| `--robot-priority` | Priority misalignment detection with confidence |
| `--robot-insights` | Full metrics: PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions, cycle breaks |
| `--robot-diff --diff-since <ref>` | Changes since ref: new/closed/modified issues |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |

#### Scoping & Filtering

```bash
bv --robot-plan --label backend              # Scope to label's subgraph
bv --robot-insights --as-of HEAD~30          # Historical point-in-time
bv --recipe actionable --robot-plan          # Pre-filter: ready to work (no blockers)
bv --recipe high-impact --robot-triage       # Pre-filter: top PageRank scores
```

### br Commands for Issue Management

```bash
br ready --json                       # Show issues ready to work (no blockers)
br list --status=open --json          # All open issues
br show <id> --json                   # Full issue details with dependencies
br create --title="..." --type=task --priority=2 --json
br update <id> --status=in_progress --json
br close <id> --reason="Completed" --json
br close <id1> <id2> --reason="Completed" --json
br sync --flush-only                  # Export DB to JSONL after Beads mutations
```

### Workflow Pattern

1. **Triage**: Run `bv --robot-triage` to find the highest-impact actionable work
2. **Claim**: Use `br update <id> --status=in_progress --json`
3. **Work**: Implement the task
4. **Complete**: Use `br close <id> --reason="Completed" --json`
5. **Sync**: Run `br sync --flush-only` after Beads mutations so the JSONL export is current

### Key Concepts

- **Dependencies**: Issues can block other issues. `br ready --json` shows only unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers 0-4, not words)
- **Types**: task, bug, feature, epic, chore, docs, question
- **Blocking**: `br dep add <issue> <depends-on>` to add dependencies

### Git Policy

`br` never commits or pushes. Follow this repository's own git instructions before staging, committing, or pushing. If the repository says "commit only when asked," that rule overrides any generic workflow advice.

<!-- end-bv-agent-instructions -->
