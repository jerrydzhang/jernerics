import os
import runpy
import sys
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any

import tomllib
from optuna.samplers import BaseSampler

ARTIFACT_ENV_VARS = [
    "JERNERICS_API_KEY",
]


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

    # Retry
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
class PueueConfig:
    parallel: int = 1


@dataclass
class ApptainerConfig:
    """Container options for ``container_type = "apptainer"``.

    Lives in its own ``[tool.jernerics.backends.<name>.apptainer]`` table rather
    than as a generic field on SharedConfig. The build_dir scratch pattern
    (stage somewhere fast, copy back) is Apptainer-specific -- Docker images
    live in the daemon store, not as files. A runtime-specific name on shared
    config was rejected; a dedicated table mirrors the slurm/pueue pattern and
    scales as Docker or other runtimes gain their own options.
    """

    build_dir: str | None = None


@dataclass
class DockerConfig:
    gpu: bool = False


@dataclass
class BackendConfig:
    shared: SharedConfig
    backend: SlurmConfig | PueueConfig | None = None  # None for LocalBackend
    container: ApptainerConfig | DockerConfig | None = None


def _read_pyproject(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigNotFound(f"Malformed pyproject.toml: {e}") from e


def _jernerics_section(data: dict) -> dict:
    return data.get("tool", {}).get("jernerics", {})


def _pyproject_dirs(start_dir: str | Path) -> list[Path]:
    current = Path(start_dir).resolve()
    dirs: list[Path] = []
    while True:
        if (current / "pyproject.toml").exists():
            dirs.append(current)
        if current == current.parent:
            break
        current = current.parent
    dirs.reverse()
    return dirs


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _resolve_tool_config(start_dir: str | Path) -> dict:
    merged: dict = {}
    for directory in _pyproject_dirs(start_dir):
        data = _read_pyproject(directory / "pyproject.toml")
        section = _jernerics_section(data)
        if section:
            merged = _deep_merge(merged, section)
    return merged


def load_tracking_server(start_dir: str | Path | None = None) -> str | None:
    start = Path.cwd() if start_dir is None else Path(start_dir)
    tool_config = _resolve_tool_config(start)
    return os.environ.get("JERNERICS_TRACKING_SERVER") or tool_config.get(
        "tracking_server"
    )


def load_backend_config(
    name: str, start_dir: str | Path | None = None
) -> BackendConfig:
    start = Path.cwd() if start_dir is None else Path(start_dir)
    if not _pyproject_dirs(start):
        raise ConfigNotFound(f"No pyproject.toml found in {start}")
    tool_config = _resolve_tool_config(start)
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
        heartbeat_interval_s=bc.get("heartbeat_interval_s", 60),
        stale_after_s=bc.get("stale_after_s", 120),
        grace_period_s=bc.get("grace_period_s", 120),
        max_retries=bc.get("max_retries", 3),
        chain_depth_cap=bc.get("chain_depth_cap", 20),
    )

    backend_specific: SlurmConfig | PueueConfig | None = None
    if backend_type == "slurm":
        slurm = bc.get("slurm", {})
        backend_specific = SlurmConfig(
            partition=slurm.get("partition", "priority"),
            time=_normalize_time(slurm.get("time", "1:00:00")),
            mem=slurm.get("mem", "16G"),
            cpus=slurm.get("cpus", 4),
            max_concurrent_jobs=slurm.get("max_concurrent_jobs", 10),
        )
    elif backend_type == "pueue":
        backend_specific = PueueConfig(
            parallel=bc.get("parallel", 1),
        )

    container_config: ApptainerConfig | DockerConfig | None = None
    if shared.container_type == "apptainer":
        apptainer = bc.get("apptainer", {})
        container_config = ApptainerConfig(
            build_dir=apptainer.get("build_dir"),
        )
    elif shared.container_type == "docker":
        docker = bc.get("docker", {})
        container_config = DockerConfig(
            gpu=docker.get("gpu", False),
        )

    return BackendConfig(
        shared=shared,
        backend=backend_specific,
        container=container_config,
    )


def find_pyproject_dir(start_dir: str | Path | None = None) -> Path | None:
    start = Path.cwd() if start_dir is None else Path(start_dir)
    current = start.resolve()
    jernerics_root: Path | None = None
    nearest: Path | None = None
    while True:
        pyproject = current / "pyproject.toml"
        if pyproject.exists():
            if nearest is None:
                nearest = current
            try:
                data = _read_pyproject(pyproject)
            except ConfigNotFound:
                data = {}
            if _jernerics_section(data):
                jernerics_root = current
        if current == current.parent:
            break
        current = current.parent
    return jernerics_root or nearest


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
        grid=module_ns.get("grid", None),
    )

    if sweep.grid is not None:
        n_combos = 1
        for values in sweep.grid.values():
            n_combos *= len(values)
        if sweep.n_trials == 1:
            sweep.n_trials = n_combos

    return sweep
