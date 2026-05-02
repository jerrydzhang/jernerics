from jernerics.backend.backend import Backend
from jernerics.backend.container import (
    Apptainer,
    Docker,
    NoContainer,
)
from jernerics.backend.path_resolver import PathResolver
from jernerics.config import ApptainerConfig, BackendConfig


def _make_container(container_type: str, project_name: str = ""):
    if container_type == "apptainer":
        return Apptainer()
    elif container_type == "docker":
        return Docker(image_name=project_name or "container.sif")
    elif container_type == "none":
        return NoContainer()
    return Apptainer()


def make_adapter(config: BackendConfig, *, host):
    backend_type = config.shared.type
    if backend_type == "slurm":
        from jernerics.backend.slurm.adapter import SlurmAdapter

        return SlurmAdapter.from_config(config, host=host)
    elif backend_type == "pueue":
        from jernerics.backend.pueue.adapter import PueueAdapter

        return PueueAdapter.from_config(config, host=host)
    raise ValueError(f"Unknown backend type: {backend_type}")


def make_backend(
    config: BackendConfig,
    *,
    host,
    syncer=None,
    tracking_server: str | None = None,
    project_name: str = "",
) -> Backend:
    container = _make_container(config.shared.container_type, project_name=project_name)
    adapter = make_adapter(config, host=host)

    shared = config.shared
    remote_dir = shared.remote_dir.replace("~", host.home)
    cache_dir = (
        shared.cache_dir.replace("~", host.home)
        if shared.cache_dir
        else f"{host.home}/.cache/jernerics"
    )

    build_dir = None
    if isinstance(config.container, ApptainerConfig):
        build_dir = config.container.build_dir
        if build_dir:
            build_dir = build_dir.replace("~", host.home)

    paths = PathResolver(
        remote_dir=remote_dir,
        cache_dir=cache_dir,
        container=container,
        build_dir=build_dir,
        project_name=project_name,
    )

    return Backend(
        host=host,
        container=container,
        adapter=adapter,
        syncer=syncer,
        paths=paths,
        remote_dir=remote_dir,
        cache_dir=cache_dir,
        project_name=project_name,
        tracking_server=tracking_server,
        heartbeat_interval_s=shared.heartbeat_interval_s,
        stale_after_s=shared.stale_after_s,
        grace_period_s=shared.grace_period_s,
        max_retries=shared.max_retries,
        chain_depth_cap=shared.chain_depth_cap,
    )
