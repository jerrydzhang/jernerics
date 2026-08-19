from pathlib import Path

from jernerics.backend.backend import Backend
from jernerics.backend.host import LocalHost, SSHHost
from jernerics.backend.project_sync import ProjectSync
from jernerics.config import (
    ConfigNotFound,
    ExitCode,
    find_pyproject_dir,
    get_project_name,
    load_backend_config,
    load_tracking_server,
)


def _get_backend(backend_name: str) -> tuple[Backend, str, Path]:
    """Load a backend by name. Returns (backend, project_name, project_dir)."""
    from jernerics.backend.factory import make_backend

    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        config = load_backend_config(backend_name)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_name = get_project_name(project_dir)
    tracking_server = load_tracking_server()

    remote_dir = config.shared.remote_dir.replace("{project_name}", project_name)
    remote_dir = remote_dir.replace("{project-name}", project_name)

    if config.shared.host:
        host = SSHHost(config.shared.host)
        syncer = ProjectSync(host, remote_dir)
    else:
        host = LocalHost()
        syncer = None

    backend = make_backend(
        config,
        host=host,
        syncer=syncer,
        tracking_server=tracking_server,
        project_name=project_name,
    )

    return backend, project_name, project_dir
