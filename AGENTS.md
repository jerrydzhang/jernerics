# AGENTS.md

## Commands

All commands run from `packages/jernerics/` unless noted.

```bash
# Setup
uv sync
. .venv/bin/activate

# Test
pytest                                          # All tests
pytest tests/unit/                               # Unit tests only
pytest tests/unit/dag/test_task.py               # Specific file
pytest tests/unit/dag/test_task.py::TestTaskDecorator::test_task_decorator_returns_task  # Single test
pytest -x                                       # Stop on first failure

# Lint & format (must pass before committing)
ruff check .                                    # Lint
ruff check . --fix                              # Auto-fix lint issues
ruff format .                                   # Format
ruff format . --check                           # Check formatting without changes

# Type check
ty check

# All checks at once
ruff check . && ruff format --check . && pytest
```

## Project Structure

uv workspace monorepo at repo root:

```
packages/
  jernerics-proto/       # Proto schema + generated code (pb2, pb2_grpc)
  jernerics/             # Client library (backend, DAG, CLI, tracking)
  jernerics-server/      # gRPC service, DuckDB store, dashboard
```

Source layout inside `packages/jernerics/`:

```
src/jernerics/
  cli.py                 # Typer CLI — all commands
  config.py              # BackendConfig, SweepConfig, config loading
  runner.py              # Trial runner invoked via python -m jernerics.runner
  paths.py               # cache_dir(), work(), is_hpc()
  dag/                   # DAG executor, task decorator, state, provenance
  backend/               # Multi-backend execution
    slurm_backend.py     # SlurmBackend (sbatch + Apptainer)
    models.py            # JobSpec, JobInfo dataclasses
    components/          # Composable primitives
      host.py            # Host protocol, LocalHost, SSHHost
      container.py       # ContainerRuntime protocol, NoContainer, Docker, Apptainer
      project_sync.py    # FileSyncer (tar/scp project sync)
  container/
    templates.py         # Container definition templates
  tracking/              # Protobuf tracker, wire format, gRPC sync client, replay
tests/
  unit/                  # Mirrors src/ structure
```

Use `uv run` from `packages/jernerics/` — not `pip`, not bare `python`.

## Definition of Done

A task is complete when ALL of the following pass:

1. `ruff check .` exits 0
2. `ruff format --check .` exits 0
3. `pytest` exits 0 with no failures
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

## When Blocked

- If tests fail after 3 attempts: stop and report the failing test with full output
- If a dependency is missing: check `pyproject.toml` first, then ask
- If you encounter an import error: verify the file exists and the module name matches — recent renames may not be in your training data
- **Never:** delete files to resolve errors, skip tests, modify test configuration files, or add `# type: ignore` / `# noqa` without justification

## Packages

Proto regeneration (from repo root):

```bash
cd packages/jernerics-proto && uv run python generate.py
```

Generated protobuf files are excluded from ruff via `extend-exclude` in root `pyproject.toml`. Do not add `# noqa` or `# type: ignore` to generated files.
