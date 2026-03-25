import os
import runpy
import sys
import tomllib
from enum import IntEnum
from importlib import resources
from pathlib import Path
from typing import Any


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


class NoConfigsFound(Exception):
    pass


class NoContainerFound(Exception):
    pass


class ConfigNotFound(Exception):
    pass


class HpcConfig:
    host: str | None
    remote_dir: str
    partition: str
    time: str
    mem: str
    cpus: int
    max_concurrent_jobs: int
    build_tmpdir: str | None

    def __init__(
        self,
        host: str | None = None,
        remote_dir: str = "~/experiments/{project_name}",
        partition: str = "priority",
        time: str = "1:00:00",
        mem: str = "16G",
        cpus: int = 4,
        max_concurrent_jobs: int = 10,
        build_tmpdir: str | None = None,
    ):
        self.host = host
        self.remote_dir = remote_dir
        self.partition = partition
        self.time = time
        self.mem = mem
        self.cpus = cpus
        self.max_concurrent_jobs = max_concurrent_jobs
        self.build_tmpdir = build_tmpdir


class ShellConfig:
    partition: str | None
    cpus: int | None
    mem: str | None
    gpu: int
    time: str | None

    def __init__(
        self,
        partition: str | None = None,
        cpus: int | None = None,
        mem: str | None = None,
        gpu: int = 0,
        time: str | None = None,
    ):
        self.partition = partition
        self.cpus = cpus
        self.mem = mem
        self.gpu = gpu
        self.time = time


def load_jernerics_config(project_dir: str | Path) -> tuple[HpcConfig, ShellConfig]:
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

    hpc = HpcConfig(
        host=os.environ.get("JERNERICS_HPC_HOST") or hpc_config.get("host"),
        remote_dir=hpc_config.get("remote_path")
        or hpc_config.get("remote_dir", "~/experiments/{project_name}"),
        partition=container_config.get("partition", "priority"),
        time=container_config.get("time", "1:00:00"),
        mem=container_config.get("mem", "16G"),
        cpus=container_config.get("cpus", 4),
        max_concurrent_jobs=safety_config.get("max_concurrent_jobs", 10),
        build_tmpdir=hpc_config.get("build_tmpdir"),
    )

    shell = ShellConfig(
        partition=shell_config.get("partition"),
        cpus=shell_config.get("cpus"),
        mem=shell_config.get("mem"),
        gpu=shell_config.get("gpu", 0),
        time=shell_config.get("time"),
    )

    return hpc, shell


def find_pyproject_dir(start_dir: str | Path | None = None) -> Path | None:
    if start_dir is None:
        start_dir = Path.cwd()
    else:
        start_dir = Path(start_dir)

    current = start_dir.resolve()
    while current != current.parent:
        pyproject = current / "pyproject.toml"
        if pyproject.exists():
            return current
        current = current.parent

    return None


def load_config(
    config_file: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], int | None, str | None]:
    config_path = Path(config_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    if not config_path.is_file():
        raise FileNotFoundError(f"Config path is not a file: {config_file}")

    try:
        module_ns = runpy.run_path(str(config_path))
    except (SyntaxError, ImportError, PermissionError) as e:
        raise RuntimeError(f"Failed to load config file '{config_file}': {e}") from e

    configs = module_ns.get("configs", [])
    if not configs:
        raise NoConfigsFound("No 'configs' list found in configuration file.")

    slurm = module_ns.get("slurm", {})
    max_workers = module_ns.get("max_workers", None)
    executor_type = module_ns.get("executor_type", None)

    return slurm, configs, max_workers, executor_type


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
