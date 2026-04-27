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
  jernerics/             # Client library (HPC, DAG, CLI, tracking)
  jernerics-server/      # gRPC service, DuckDB store, dashboard
```

Source layout inside `packages/jernerics/`:

```
src/jernerics/
  cli.py                 # Typer CLI — all commands
  config.py              # HpcConfig, SweepConfig, config loading
  runner.py              # Trial runner invoked via python -m jernerics.runner
  paths.py               # Cache dir, work dir, bind resolution
  dag/                   # DAG executor, task decorator, state, provenance
  hpc/                   # SSH client, SLURM job manager, project sync
  container/             # Container builder (SLURM submit + apptainer)
  tracking/              # Protobuf tracker, wire format, gRPC sync client, replay
tests/
  unit/                  # Mirrors src/ structure
  integration/
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

- **SSH commands** (via `_quote_path()`): `~` expands. Use it directly: `ssh.mkdir("~/projects/foo")`
- **SLURM directives, quoted strings in scripts, heredocs**: `~` does NOT expand. Use `$HOME`: `remote_dir.replace("~", "$HOME")`
- **Path arguments to `subprocess.run(["ssh", ...])`**: the remote shell expands `~` — safe to use.

### Container-aware code

`cli.py` generates bash scripts that run inside apptainer containers on HPC. When editing script generation:
- The container sees `/work` (project source) and `/cache` (ephemeral data). Never hardcode host paths in generated scripts.
- The runner is invoked as `python -m jernerics.runner` — it runs in the container's Python, not the local one.
- `run local` and `run slurm` execute the same runner code. If something works locally but fails on HPC (or vice versa), the difference is the environment, not the code.

### Two config layers

1. **`pyproject.toml` `[tool.jernerics.*]`** — infrastructure (HPC host, SLURM defaults, bind mounts). Loaded by `load_jernerics_config()`.
2. **User's experiment config** (Python file with `base`, `search_space`, `n_trials`, etc.). Loaded by `load_config()` via `runpy.run_path()`.

When debugging config issues, check which layer owns the value.

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
