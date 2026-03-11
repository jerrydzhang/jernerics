import runpy
from importlib import resources
from pathlib import Path
from typing import Any


class NoConfigsFound(Exception):
    pass


def load_config(config_file: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config_path = Path(config_file)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    module_ns = runpy.run_path(str(config_path))

    configs = module_ns.get("configs", [])
    if not configs:
        raise NoConfigsFound("No 'configs' list found in configuration file.")

    slurm = module_ns.get("slurm", {})

    return slurm, configs


def get_script_path(script_name: str, script_module: str = "jernerics.scripts") -> str:
    return str(resources.files(script_module).joinpath(script_name))
