# AGENTS.md - Jernerics Codebase Guide

This document provides guidelines for agentic coding agents working in this repository.

## Project Overview

Jernerics is a Python 3.12+ toolkit for building and evaluating ML models, providing utilities for DAG-based experiment execution, HPC cluster management via SLURM, and container-based reproducibility.

## Build/Lint/Test Commands

### Setup
```bash
uv sync                    # Install all dependencies
. .venv/bin/activate       # Activate virtual environment
```

### Testing
```bash
pytest                                      # Run all tests
pytest tests/unit/                          # Run unit tests only
pytest tests/integration/                   # Run integration tests only
pytest tests/unit/dag/test_task.py          # Run specific test file
pytest tests/unit/dag/test_task.py::TestTaskDecorator::test_task_decorator_returns_task  # Run single test
pytest -x                                   # Stop on first failure
pytest -v                                   # Verbose output
pytest --cov=src                            # With coverage
```

### Linting & Formatting
```bash
ruff check .                # Run linter
ruff check . --fix          # Auto-fix lint issues
ruff format .               # Format code
ruff format . --check       # Check formatting without changes
ty check                    # Type check with ty
```

### Pre-commit
```bash
pre-commit run --all-files  # Run all pre-commit hooks
```

## Code Style Guidelines

### Imports

```python
from __future__ import annotations  # Always first for modern typing

import standard_library_modules
import third_party_modules
from local_modules import ...
```

Order: future annotations → standard library → third-party → local imports. Group imports logically, use absolute imports for project modules.

### Formatting

- **Line length**: 88 characters (ruff default)
- **Quotes**: Prefer double quotes for strings
- **Indentation**: 4 spaces (no tabs)
- **Trailing commas**: Use in multi-line collections

### Type Hints

```python
from __future__ import annotations
from typing import Any

def function(
    config: dict[str, Any],
    path: str | Path | None = None,
) -> dict[str, Any]:
    ...
```

- Use `from __future__ import annotations` for modern type syntax
- Use `|` for unions instead of `Union[]`
- Use `list[X]`, `dict[K, V]` instead of `List`, `Dict`
- Always annotate function parameters and return types
- Use `Any` sparingly; prefer specific types

### Naming Conventions

```python
class MyClass:                    # PascalCase for classes
    def my_method(self): ...      # snake_case for methods/functions

my_variable = 1                   # snake_case for variables
MY_CONSTANT = 1                   # UPPER_SNAKE_CASE for constants

def _private_function(): ...      # Underscore prefix for internal functions

class HpcConfig: ...              # No abbreviations in class names
class SSHClient: ...              # Acronyms kept uppercase
```

### Data Structures

```python
from dataclasses import dataclass, field

@dataclass
class Task:
    name: str
    func: Callable[..., Any]
    depends_on: list[Task] = field(default_factory=list)
```

- Use `@dataclass` for data-holding classes
- Use `field(default_factory=...)` for mutable defaults

### Error Handling

```python
class ConfigNotFound(Exception):
    pass

def load_config(config_file: str) -> SweepConfig:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")
```

- Define custom exceptions at module level
- Use descriptive error messages with context
- Raise specific exception types (not generic `Exception`)

### CLI Patterns

```python
import typer
from typing_extensions import Annotated

app = typer.Typer(help="Description.")

@app.command()
def command(
    arg: Annotated[str, typer.Argument(help="Argument description.")],
    option: Annotated[str | None, typer.Option("--option", "-o", help="Option desc.")] = None,
):
    ...
```

## Testing Guidelines

### Test Organization
```
tests/
├── conftest.py              # Shared fixtures
├── unit/                    # Unit tests (fast, isolated)
│   ├── dag/
│   ├── hpc/
│   └── container/
└── integration/             # Integration tests (slower, may use external resources)
```

### Test Structure

```python
from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from jernerics.dag import DAG, task


class TestFeatureName:
    def test_basic_case(self):
        ...

    def test_edge_case(self, tmp_path):  # Use pytest fixtures
        ...

    @given(st.integers(), st.integers())  # Property-based testing
    def test_with_various_inputs(self, a, b):
        ...
```

### Test Patterns

```python
class TestDAGValidation:
    def test_validate_missing_dependency_raises(self):
        @task
        def a(config):
            return 1

        dag = DAG()
        dag.add_task(a)

        try:
            dag.validate()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "unregistered task" in str(e)
```

- Group related tests in classes
- Use descriptive test names: `test_<what>_<condition>`
- Use hypothesis for property-based testing
- Use `tmp_path` fixture for filesystem tests

## Architecture

### Code Generation (cli.py)

The core of `cli.py` is **code generation**: functions like `_get_runner_code()` and `_get_sweep_runner_code()` return Python source code as strings. This code is embedded in bash SLURM scripts via heredoc and runs **inside the apptainer container on HPC**.

Implications when editing these functions:
- You are writing Python that becomes a string literal. Double braces `{{}}` produce literal braces in output.
- Changes to generated code take effect only after the container is rebuilt and redeployed. Use `jernerics run local` for quick iteration (it runs the same generated code via `python -c` in a subprocess).
- The generated code imports `jernerics.dag` and the user's DAG file — it runs in the container's Python environment, not the local one.
- `run local` and `run slurm` execute the same generated code paths. If something works locally but fails on HPC (or vice versa), the difference is in the environment, not the code.

### Two-Layer Config System

Jernerics has two config layers with different purposes and loading mechanisms:

**1. `pyproject.toml` `[tool.jernerics.*]`** — Infrastructure config, loaded by `load_jernerics_config()`.
Parsed into dataclasses (`HpcConfig`, `ShellConfig`, `BindsConfig`). Can be overridden by environment variables (`JERNERICS_HPC_HOST`).

Sections: `hpc` (host, remote dir, cache), `container` (SLURM resource limits for build/run), `safety` (max concurrent jobs), `binds` (container bind mounts), `shell` (interactive shell defaults).

**2. `config.py` (user's experiment config)** — Experiment config, loaded by `load_config()` via `runpy.run_path()`.
Defines `base`, `search_space`, `n_trials`, `sampler`, `objective`, `direction`, `slurm` overrides. Returns a `SweepConfig` dataclass.

When debugging config issues, check which layer owns the value you're looking for.

### Container Builds

`jernerics container build` submits a SLURM job that runs `apptainer build --fakeroot` on a compute node. It syncs the project to HPC, writes a build script, and submits it. The container must be rebuilt whenever `uv.lock` changes (tracked via `needs_rebuild()`). On HPC, containers are `.sif` files; locally they can be `.tar.gz` docker archives (see `find_container()` for the lookup order).

## Project-Specific Patterns

### DAG Tasks

```python
from jernerics.dag import DAG, task

# Preferred: context manager auto-registers tasks
with DAG() as dag:

    @task
    def setup(config):
        return {"done": True}

    @task(depends_on=[setup])
    def train(setup, config):
        return setup["done"]

# Alternative: manual registration
dag = DAG()

@task
def setup(config):
    return {"done": True}

dag.add_task(setup)
```

### Configuration Files

```python
import optuna
from jernerics.dag.executor import ThreadPoolRunner, SyncRunner

base = {"seed": 42, "model": "gpt"}

def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-5, 1e-1, log=True),
        "batch_size": trial.suggest_int("batch_size", 16, 128),
    }

n_trials = 50
sampler = optuna.samplers.TPESampler(seed=42)

def objective(results):
    return results["train"].value["loss"]

direction = "minimize"

slurm = {
    "partition": "priority",
    "time": "1:00:00",
    "mem": "16G",
}

runner = ThreadPoolRunner(max_workers=4)  # or SyncRunner() for debugging
```

For single runs, omit `search_space` and set `n_trials = 1` (default). `load_config()` returns a `SweepConfig` dataclass.

## Executor API

The executor uses the Runner protocol. Two implementations:

- `ThreadPoolRunner(max_workers=None)` — production, uses `FIRST_COMPLETED` for responsive scheduling
- `SyncRunner()` — debugging (pdb), runs tasks inline with no threads

`execute_dag()` returns `dict[str, TaskResult]` where `TaskResult` has `.value`, `.error`, `.error_traceback`, and `.is_error` property. `DAG.run()` and `DAG.resume()` return the same type.

Users pass a runner instance in their config file:
```python
from jernerics.dag.executor import ThreadPoolRunner
runner = ThreadPoolRunner(max_workers=4)
```

Serial execution is for debugging only. Not a first-class strategy.

## HPC Environment Constraints

**CRITICAL: Tilde (`~`) Expansion Rules**

`~` expansion only happens by the shell in specific contexts. Understanding when it works is essential:

**`~` DOES expand (use it directly):**
- SSH commands via `_quote_path()` helper - the remote shell expands it
- Example: `ssh.mkdir("~/projects/foo")` works correctly

**`~` DOES NOT expand (use `$HOME` instead):**
- SLURM `--output`/`--error` directives (not processed by shell)
- Double-quoted strings in bash scripts (e.g., `"${PATH}:~/bin"` - ~ is literal)
- Non-interactive shell scripts when paths are embedded in heredocs or quoted

**Wrong (SLURM directive with ~):**
```python
f"#SBATCH --output={remote_dir}/build_%j.out"  # remote_dir = "~/projects/foo"
```

**Correct (use $HOME for SLURM):**
```python
slurm_dir = remote_dir.replace("~", "$HOME")
f"#SBATCH --output={slurm_dir}/build_%j.out"  # "$HOME/projects/foo"
```

**Wrong (bind path in double quotes inside SLURM script):**
```python
bind_args.append(f'"{cache_path}:{container_path}"')  # cache_path = "~/cache"
# Inside script: "~/cache:/work/cache" - ~ is literal inside ""
```

**Correct (use $HOME for bind paths):**
```python
cache_path = cache_path.replace("~", "$HOME")
bind_args.append(f'"{cache_path}:{container_path}"')  # "$HOME/cache:/work/cache"
```

**Correct (SSH commands via _quote_path):**
```python
ssh.mkdir("~/projects/foo")  # Works - _quote_path preserves ~ for remote shell
```

The `_quote_path()` helper in `src/jernerics/hpc/ssh.py` preserves `~` for SSH commands where the remote shell will expand it. For all other contexts (SLURM directives, quoted strings in scripts), replace `~` with `$HOME`.

## Important Notes

- **No comments on self-documenting code** - Avoid redundant comments
- **Pre-commit hooks run tests** - Ensure tests pass before committing
- **Use uv** - This project uses uv, not pip directly
- **Python 3.12+** - Use modern Python features
