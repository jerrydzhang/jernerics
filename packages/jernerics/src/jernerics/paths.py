import os
from pathlib import Path

from jernerics.config import (
    find_pyproject_dir,
    get_project_name,
)

CACHE_MOUNT = "/cache"


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

    Resolution depends on where the caller runs:

    - Host: ``~/.cache/jernerics/<project_name>/``
    - Container job (``JERNERICS_HPC`` set): the ``/cache`` bind mount,
      already project-scoped by the backend

    Layout under the resolved root::

        optuna/<study>.journal
        tracking/<study>/0.jsonl
        jobs/<job_id>.json
    """
    if is_hpc():
        return Path(CACHE_MOUNT)

    project_dir = find_pyproject_dir()
    if project_dir is None:
        return Path.home() / ".cache" / "jernerics"

    project_name = get_project_name(project_dir)
    return Path.home() / ".cache" / "jernerics" / project_name
