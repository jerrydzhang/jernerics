from __future__ import annotations

import os
from pathlib import Path

from jernerics._cli_helpers import (
    ConfigNotFound,
    find_pyproject_dir,
    get_project_name,
    load_jernerics_config,
)


class BindNotFound(Exception):
    pass


def is_hpc() -> bool:
    return os.environ.get("JERNERICS_HPC", "").lower() in ("1", "true", "yes")


def work() -> Path:
    project_dir = find_pyproject_dir()
    if project_dir is None:
        return Path.cwd()

    if is_hpc():
        return Path("/work")

    return project_dir


def bind(name: str) -> Path:
    project_dir = find_pyproject_dir()
    if project_dir is None:
        raise BindNotFound(
            f"Cannot resolve bind '{name}': "
            "no pyproject.toml found in current directory or parents"
        )

    try:
        hpc_config, _, binds = load_jernerics_config(project_dir)
    except ConfigNotFound as e:
        raise BindNotFound(f"Cannot resolve bind '{name}': {e}") from e

    container_path = None
    for ctr_path, cache_subdir in binds.items():
        if cache_subdir == name:
            container_path = ctr_path
            break

    if container_path is None:
        available = list(binds.values())
        raise BindNotFound(
            f"Bind '{name}' not found in [tool.jernerics.binds]. Available: {available}"
            if available
            else "No binds configured."
        )

    if is_hpc():
        return Path(container_path)

    project_name = get_project_name(project_dir)
    if hpc_config.cache_dir:
        cache_base = hpc_config.cache_dir.replace("{project_name}", project_name)
        cache_base = cache_base.replace("{project-name}", project_name)
        cache_path = Path(cache_base).expanduser() / project_name / name
    else:
        cache_path = Path.home() / ".cache" / "jernerics" / project_name / name

    cache_path.mkdir(parents=True, exist_ok=True)
    return cache_path
