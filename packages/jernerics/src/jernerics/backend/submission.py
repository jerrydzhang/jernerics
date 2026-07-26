import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.command_builders import build_sweep_commands
from jernerics.backend.container import Docker, NoContainer
from jernerics.backend.models import SubmitResult
from jernerics.backend.path_resolver import PathResolver
from jernerics.config import (
    ARTIFACT_ENV_VARS,
    ApptainerConfig,
    BackendConfig,
    DockerConfig,
    _normalize_time,
)
from jernerics.retry import RetryContext


def _make_container(container_type: str, project_name: str = "", *, gpu: bool = False):
    if container_type == "apptainer":
        from jernerics.backend.container import Apptainer

        return Apptainer()
    elif container_type == "docker":
        return Docker(image_name=project_name or "container.sif", gpu=gpu)
    elif container_type == "none":
        return NoContainer()
    from jernerics.backend.container import Apptainer

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


@dataclass
class SweepInfrastructure:
    adapter: Any
    container: Any
    paths: PathResolver


def assemble_infrastructure(
    config: BackendConfig,
    *,
    host,
    project_name: str = "",
) -> SweepInfrastructure:
    gpu = isinstance(config.container, DockerConfig) and config.container.gpu
    container = _make_container(
        config.shared.container_type,
        project_name=project_name,
        gpu=gpu,
    )
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

    return SweepInfrastructure(
        adapter=adapter,
        container=container,
        paths=paths,
    )


def submit_sweep(
    spec,
    infra: SweepInfrastructure,
    *,
    host,
    project_dir: str | Path,
    project_name: str,
    backend_name: str,
    direction: str,
    tracking_server: str | None = None,
    cli_overrides: dict[str, str] | None = None,
    experiment_overrides: dict[str, Any] | None = None,
    heartbeat_interval_s: float = 60.0,
    dry_run: bool = False,
    chain_depth: int = 0,
    artifact_env: dict[str, str] | None = None,
) -> SubmitResult | None:
    # Merge overrides
    merged = {
        **{
            k: _normalize_time(v) if k == "time" else v
            for k, v in (experiment_overrides or {}).items()
        },
        **{
            k: _normalize_time(v) if k == "time" else v
            for k, v in (cli_overrides or {}).items()
        },
    }
    merged = {k: v for k, v in merged.items() if v is not None}

    max_parallel_raw = merged.pop("max_parallel", None)
    try:
        max_parallel = int(max_parallel_raw) if max_parallel_raw else None
    except (ValueError, TypeError) as e:
        raise ValueError(
            f"max_parallel must be an integer, got: {max_parallel_raw!r}"
        ) from e

    cache_host = infra.paths.resolve_cache()
    artifact_env_resolved = (
        artifact_env
        or {k: v for k in ARTIFACT_ENV_VARS if (v := os.environ.get(k))}
        or None
    )

    # Write retry context
    retry_dir_host = f"{cache_host}/retry"
    host.mkdir(retry_dir_host)
    retry_ctx_path = infra.paths.retry_ctx_path(spec.study_name)
    retry_ctx = RetryContext(
        study_name=spec.study_name,
        backend_name=backend_name,
        trial_relpath=spec.trial_relpath,
        config_relpath=spec.config_relpath,
        cli_overrides=cli_overrides or {},
        storage_path=spec.storage_url,
        tracking_dir=infra.paths.tracking_dir(spec.study_name),
        project_dir=infra.paths.work_prefix,
        project_name=project_name,
        host_home=host.home,
        git_hash=spec.git_hash or "",
        server_addr=tracking_server or "",
        ctx_path=retry_ctx_path,
        chain_depth=chain_depth,
    )
    host_ctx_path = f"{cache_host}/retry/{spec.study_name}_ctx.json"
    host.write_file(host_ctx_path, retry_ctx.to_json())

    wrapped_setup, wrapped_trial, post_hook = build_sweep_commands(
        spec,
        infra.container,
        infra.paths,
        direction=direction,
        tracking_server=tracking_server,
        heartbeat_interval_s=heartbeat_interval_s,
        git_hash=spec.git_hash,
        multiline=not dry_run,
        retry_ctx_path=retry_ctx_path,
        chain_depth=chain_depth,
        artifact_env=artifact_env_resolved,
    )

    params = SweepSubmissionParams(
        setup_command=wrapped_setup,
        trial_command=wrapped_trial,
        post_hook_command=post_hook,
        n_trials=spec.n_trials,
        study_name=spec.study_name,
        log_dir=f"{cache_host}/logs",
        cache_dir=cache_host,
        max_parallel=max_parallel,
        overrides=merged,
    )

    if dry_run:
        return infra.adapter.render_sweep(params)

    return infra.adapter.submit_sweep(params)
