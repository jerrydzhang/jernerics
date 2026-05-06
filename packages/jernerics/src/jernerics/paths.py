import os
from pathlib import Path

from jernerics.config import (
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
    Resolve the local cache directory for a project.

    Layout::

        ~/.cache/jernerics/<project_name>/
          optuna/<study>.journal
          tracking/<study>/0.pb
          jobs/<job_id>.json
    """
    project_dir = find_pyproject_dir()
    if project_dir is None:
        return Path.home() / ".cache" / "jernerics"

    project_name = get_project_name(project_dir)
    return Path.home() / ".cache" / "jernerics" / project_name
