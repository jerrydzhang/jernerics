from __future__ import annotations

import os
from pathlib import Path

from jernerics._cli_helpers import (
    find_pyproject_dir,
    get_project_name,
    load_jernerics_config,
)


def _is_hpc() -> bool:
    return os.environ.get("JERNERICS_HPC", "").lower() in ("1", "true", "yes")


def _get_cache_dir() -> Path:
    project_dir = find_pyproject_dir()
    if project_dir is None:
        return Path.home() / ".cache" / "jernerics"

    project_name = get_project_name(project_dir)

    if _is_hpc():
        try:
            hpc_config, _, _ = load_jernerics_config(project_dir)
            if hpc_config.cache_dir:
                cache_base = hpc_config.cache_dir.replace(
                    "{project_name}", project_name
                )
                return Path(cache_base) / project_name
        except Exception:
            pass
        return Path("/cache") / project_name

    return Path.home() / ".cache" / "jernerics" / project_name


def _get_work_dir() -> Path:
    project_dir = find_pyproject_dir()
    if project_dir is None:
        return Path.cwd()

    if _is_hpc():
        return Path("/work")

    return project_dir


class _Paths:
    @property
    def cache(self) -> Path:
        return _get_cache_dir()

    @property
    def work(self) -> Path:
        return _get_work_dir()

    @property
    def is_hpc(self) -> bool:
        return _is_hpc()


paths = _Paths()
