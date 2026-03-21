import runpy
from importlib import resources
from pathlib import Path
from typing import Any

DEFAULT_CONTAINER_SIF = ".jernerics/container.sif"
DEFAULT_CONTAINER_TAR = ".jernerics/container.tar.gz"


class NoConfigsFound(Exception):
    pass


class NoContainerFound(Exception):
    pass


def load_config(
    config_file: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], int | None]:
    config_path = Path(config_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    module_ns = runpy.run_path(str(config_path))

    configs = module_ns.get("configs", [])
    if not configs:
        raise NoConfigsFound("No 'configs' list found in configuration file.")

    slurm = module_ns.get("slurm", {})
    max_workers = module_ns.get("max_workers", None)

    return slurm, configs, max_workers


def get_script_path(script_name: str, script_module: str = "jernerics.scripts") -> str:
    return str(resources.files(script_module).joinpath(script_name))


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
