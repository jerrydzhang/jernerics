import os
import runpy
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from importlib import resources
from pathlib import Path
from typing import Any

import tomllib
from optuna.samplers import BaseSampler

from .dag import Runner


class ExitCode(IntEnum):
    SUCCESS = 0
    GENERAL_ERROR = 1
    SSH_ERROR = 2
    CONFIG_ERROR = 3
    SLURM_ERROR = 4
    CONTAINER_ERROR = 5


def is_tty() -> bool:
    return sys.stdout.isatty()


@dataclass
class SweepConfig:
    base: dict[str, Any]
    search_space: Callable[..., dict[str, Any]] | None
    n_trials: int
    sampler: BaseSampler | None
    objective: Callable[..., float] | None
    direction: str
    backend_overrides: dict[str, dict[str, Any]]
    runner: Runner | None
    grid: dict[str, list] | None = None


class ConfigNotFound(Exception):
    pass


def _normalize_time(value: str | None) -> str | None:
    if isinstance(value, str) and value.lower() == "none":
        return None
    return value


@dataclass
class SharedConfig:
    name: str
    type: str  # "slurm" | "bare" | ...

    # SSH backends
    host: str | None = None
    remote_dir: str = "~/experiments/{project_name}"
    cache_dir: str | None = None

    # Container
    container_type: str = "apptainer"  # "apptainer" | "docker" | "none"

    # Pueue (future)
    parallel: int = 1

    # Auto-retry
    auto_retry: bool = False
    heartbeat_interval_s: int = 60
    stale_after_s: int = 120
    grace_period_s: int = 120
    max_retries: int = 3
    chain_depth_cap: int = 20


@dataclass
class SlurmConfig:
    partition: str = "priority"
    time: str | None = "1:00:00"
    mem: str = "16G"
    cpus: int = 4
    max_concurrent_jobs: int = 10

    def defaults_dict(self) -> dict[str, str | None]:
        return {
            "partition": self.partition,
            "time": self.time,
            "mem": self.mem,
        }


@dataclass
class BackendConfig:
    shared: SharedConfig
    backend: SlurmConfig | None = None  # None for LocalBackend


def _load_tool_config(project_dir: str | Path) -> dict:
    project_path = Path(project_dir)
    pyproject_path = project_path / "pyproject.toml"

    if not pyproject_path.exists():
        raise ConfigNotFound(f"No pyproject.toml found in {project_dir}")

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigNotFound(f"Malformed pyproject.toml: {e}") from e

    return data.get("tool", {}).get("jernerics", {})


def load_tracking_server(project_dir: str | Path) -> str | None:
    tool_config = _load_tool_config(project_dir)
    return os.environ.get("JERNERICS_TRACKING_SERVER") or tool_config.get(
        "tracking_server"
    )


def load_backend_config(name: str, project_dir: str | Path) -> BackendConfig:
    tool_config = _load_tool_config(project_dir)
    backends = tool_config.get("backends", {})

    if name not in backends:
        available = list(backends.keys())
        raise ConfigNotFound(
            f"Backend '{name}' not found in [tool.jernerics.backends]."
            f" Available: {available}"
            if available
            else "No backends configured."
        )

    bc = backends[name]
    backend_type = bc.get("type", "slurm")

    shared = SharedConfig(
        name=name,
        type=backend_type,
        host=os.environ.get("JERNERICS_HPC_HOST") or bc.get("host"),
        remote_dir=bc.get("remote_dir", "~/experiments/{project_name}"),
        cache_dir=bc.get("cache_dir"),
        parallel=bc.get("parallel", 1),
        container_type=bc.get("container_type", "apptainer"),
        auto_retry=bc.get("auto_retry", False),
        heartbeat_interval_s=bc.get("heartbeat_interval_s", 60),
        stale_after_s=bc.get("stale_after_s", 120),
        grace_period_s=bc.get("grace_period_s", 120),
        max_retries=bc.get("max_retries", 3),
        chain_depth_cap=bc.get("chain_depth_cap", 20),
    )

    backend_specific: SlurmConfig | None = None
    if backend_type == "slurm":
        slurm = bc.get("slurm", {})
        backend_specific = SlurmConfig(
            partition=slurm.get("partition", "priority"),
            time=_normalize_time(slurm.get("time", "1:00:00")),
            mem=slurm.get("mem", "16G"),
            cpus=slurm.get("cpus", 4),
            max_concurrent_jobs=slurm.get("max_concurrent_jobs", 10),
        )

    return BackendConfig(shared=shared, backend=backend_specific)


def find_pyproject_dir(start_dir: str | Path | None = None) -> Path | None:
    start_dir = Path.cwd() if start_dir is None else Path(start_dir)

    current = start_dir.resolve()
    while current != current.parent:
        pyproject = current / "pyproject.toml"
        if pyproject.exists():
            return current
        current = current.parent

    return None


def get_project_name(project_dir: str | Path) -> str:
    project_path = Path(project_dir)
    pyproject_path = project_path / "pyproject.toml"

    if pyproject_path.exists():
        try:
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
            project_name = data.get("project", {}).get("name")
            if project_name:
                return project_name
        except (tomllib.TOMLDecodeError, KeyError):
            pass

    return project_path.resolve().name


def load_config(config_file: str) -> SweepConfig:
    config_path = Path(config_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    if not config_path.is_file():
        raise FileNotFoundError(f"Config path is not a file: {config_file}")

    try:
        module_ns = runpy.run_path(str(config_path))
    except (SyntaxError, ImportError, PermissionError) as e:
        raise RuntimeError(f"Failed to load config file '{config_file}': {e}") from e

    if "configs" in module_ns and "base" not in module_ns:
        warnings.warn(
            "'configs' is no longer supported. "
            "Use 'base' for base config, 'search_space' for Optuna parameters, "
            "and 'n_trials' for the number of trials.",
            DeprecationWarning,
            stacklevel=2,
        )

    sweep = SweepConfig(
        base=module_ns.get("base", {}),
        search_space=module_ns.get("search_space", None),
        n_trials=module_ns.get("n_trials", 1),
        sampler=module_ns.get("sampler", None),
        objective=module_ns.get("objective", None),
        direction=module_ns.get("direction", "minimize"),
        backend_overrides=module_ns.get("backend_overrides", {}),
        runner=module_ns.get("runner", None),
        grid=module_ns.get("grid", None),
    )

    if sweep.grid is not None:
        n_combos = 1
        for values in sweep.grid.values():
            n_combos *= len(values)
        if sweep.n_trials == 1:
            sweep.n_trials = n_combos

    return sweep


def get_script_path(script_name: str, script_module: str = "jernerics.scripts") -> str:
    path = resources.files(script_module).joinpath(script_name)
    if not Path(str(path)).exists():
        raise FileNotFoundError(f"Script not found: {script_name}")
    return str(path)
