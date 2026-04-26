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


DEFAULT_CONTAINER_SIF = ".jernerics/container.sif"
DEFAULT_CONTAINER_TAR = ".jernerics/container.tar.gz"


@dataclass
class SweepConfig:
    base: dict[str, Any]
    search_space: Callable[..., dict[str, Any]] | None
    n_trials: int
    sampler: BaseSampler | None
    objective: Callable[..., float] | None
    direction: str
    slurm: dict[str, Any]
    runner: Runner | None


class NoContainerFound(Exception):
    pass


class ConfigNotFound(Exception):
    pass


def _normalize_time(value: str | None) -> str | None:
    if isinstance(value, str) and value.lower() == "none":
        return None
    return value


@dataclass
class HpcConfig:
    host: str | None = None
    remote_dir: str = "~/experiments/{project_name}"
    partition: str = "priority"
    time: str | None = "1:00:00"
    mem: str = "16G"
    cpus: int = 4
    max_concurrent_jobs: int = 10
    cache_dir: str | None = None
    tracking_server: str | None = None


class BindsConfig(dict[str, str]):
    pass


@dataclass
class ShellConfig:
    partition: str | None = None
    cpus: int | None = None
    mem: str | None = None
    gpu: int = 0
    time: str | None = None


def load_jernerics_config(
    project_dir: str | Path,
) -> tuple[HpcConfig, ShellConfig, BindsConfig]:
    project_path = Path(project_dir)
    pyproject_path = project_path / "pyproject.toml"

    if not pyproject_path.exists():
        raise ConfigNotFound(f"No pyproject.toml found in {project_dir}")

    try:
        with open(pyproject_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigNotFound(f"Malformed pyproject.toml: {e}") from e

    tool_config = data.get("tool", {}).get("jernerics", {})

    hpc_config = tool_config.get("hpc", {})
    container_config = tool_config.get("container", {})
    safety_config = tool_config.get("safety", {})
    shell_config = tool_config.get("shell", {})
    binds_config = tool_config.get("binds", {})

    hpc = HpcConfig(
        host=os.environ.get("JERNERICS_HPC_HOST") or hpc_config.get("host"),
        remote_dir=hpc_config.get("remote_path")
        or hpc_config.get("remote_dir", "~/experiments/{project_name}"),
        partition=container_config.get("partition", "priority"),
        time=_normalize_time(container_config.get("time", "1:00:00")),
        mem=container_config.get("mem", "16G"),
        cpus=container_config.get("cpus", 4),
        max_concurrent_jobs=safety_config.get("max_concurrent_jobs", 10),
        cache_dir=hpc_config.get("cache_dir"),
        tracking_server=os.environ.get("JERNERICS_TRACKING_SERVER")
        or hpc_config.get("tracking_server"),
    )

    shell = ShellConfig(
        partition=shell_config.get("partition"),
        cpus=shell_config.get("cpus"),
        mem=shell_config.get("mem"),
        gpu=shell_config.get("gpu", 0),
        time=shell_config.get("time"),
    )

    binds = BindsConfig(binds_config)

    return hpc, shell, binds


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

    return SweepConfig(
        base=module_ns.get("base", {}),
        search_space=module_ns.get("search_space", None),
        n_trials=module_ns.get("n_trials", 1),
        sampler=module_ns.get("sampler", None),
        objective=module_ns.get("objective", None),
        direction=module_ns.get("direction", "minimize"),
        slurm=module_ns.get("slurm", {}),
        runner=module_ns.get("runner", None),
    )


def get_script_path(script_name: str, script_module: str = "jernerics.scripts") -> str:
    path = resources.files(script_module).joinpath(script_name)
    if not Path(str(path)).exists():
        raise FileNotFoundError(f"Script not found: {script_name}")
    return str(path)


def find_container(
    explicit: str | None, no_container: bool, dag_dir: str
) -> str | None:
    if no_container:
        return None

    if explicit:
        if not Path(explicit).exists():
            raise NoContainerFound(f"Container not found: {explicit}")
        return explicit

    base_dir = Path(dag_dir)

    sif_path = base_dir / DEFAULT_CONTAINER_SIF
    if sif_path.exists():
        return str(sif_path)

    tar_path = base_dir / DEFAULT_CONTAINER_TAR
    if tar_path.exists():
        return f"docker-archive://{tar_path}"

    raise NoContainerFound(
        f"""No container found at {sif_path} or {tar_path}
Build one with:
  nix build .#container -o .jernerics/container.tar.gz
Or convert on HPC:
  apptainer build .jernerics/container.sif docker-archive://container.tar.gz
Or use --no-container to run without a container."""
    )
