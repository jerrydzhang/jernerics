import os
from pathlib import Path

from jernerics.config import (
    ConfigNotFound,
    _load_tool_config,
    find_pyproject_dir,
    get_project_name,
)


def is_hpc() -> bool:
    return os.environ.get("JERNERICS_HPC", "").lower() in ("1", "true", "yes")


def work() -> Path:
    project_dir = find_pyproject_dir()
    if project_dir is None:
        return Path.cwd()

    if is_hpc():
        return Path("/work")

    return project_dir


def cache_dir() -> Path:
    """
    Resolve the host path to the project's cache directory.

    Layout::

        <cache_root>/<project_name>/
          optuna/<study>.db
          tracking/<study>/0.pb
          logs/slurm_1234.out

    Resolution:
      - HPC: cache_dir from backend config
      - Local fallback: ~/.cache/jernerics/<project>
    """
    project_dir = find_pyproject_dir()
    if project_dir is None:
        return Path.home() / ".cache" / "jernerics"

    project_name = get_project_name(project_dir)
    try:
        tool_config = _load_tool_config(project_dir)
    except ConfigNotFound:
        return Path.home() / ".cache" / "jernerics" / project_name

    backends = tool_config.get("backends", {})
    cache_dir_value = None
    for bc in backends.values():
        if bc.get("cache_dir"):
            cache_dir_value = bc["cache_dir"]
            break

    if cache_dir_value:
        base = cache_dir_value.replace("{project_name}", project_name)
        base = base.replace("{project-name}", project_name)
        return Path(base).expanduser()

    return Path.home() / ".cache" / "jernerics" / project_name
