import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jernerics_schema import (
    JobSnapshotEvent,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrackingEvent,
    sweep_id_for,
)

from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.command_builders import build_sweep_commands
from jernerics.backend.container import Docker, NoContainer
from jernerics.backend.models import SubmitResult
from jernerics.backend.path_resolver import PathResolver, substitute_project_name
from jernerics.config import (
    ARTIFACT_ENV_VARS,
    ApptainerConfig,
    BackendConfig,
    DockerConfig,
    _normalize_time,
)
from jernerics.retry import RetryContext


def build_submission_events(spec, backend_name: str, result) -> list:
    """Deploy-path v3 events describing one sweep submission and its jobs."""
    project = spec.project_name or ""
    sweep_id = sweep_id_for(project, spec.study_name)
    now = datetime.now(UTC)
    config_source = spec.config_relpath or str(spec.config_path)
    events: list[TrackingEvent] = [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=now,
            project=project,
            sweep_id=sweep_id,
            name=spec.study_name,
            state="running",
        ),
        SubmissionSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=now,
            submission_id=uuid.UUID(spec.submission_id),
            sweep_id=sweep_id,
            backend=backend_name,
            state=SubmissionState.SUBMITTED,
            submitted_at=now,
            expected_trials=spec.n_trials or None,
            git_hash=spec.git_hash,
            config_source=config_source,
        ),
    ]
    for sub in result.submissions:
        events.append(
            JobSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=now,
                job_id=uuid.uuid4(),
                submission_id=uuid.UUID(spec.submission_id),
                scheduler_job_id=sub.job_id,
                role=sub.role,
                state=SubmissionState.SUBMITTED,
            )
        )
    return events


def write_submission_events(
    events: list, host, tracking_dir: str, filename: str
) -> None:
    """Persist submission JSONL where the sweep's post-hook replay finds it."""
    submission_dir = f"{tracking_dir}/submission"
    host.mkdir(submission_dir)
    host.write_file(
        f"{submission_dir}/{filename}",
        "".join(event.model_dump_json() + "\n" for event in events),
    )


def write_env_file(host, cache_host: str, env: dict[str, str]) -> str:
    """Provision container env vars as a 0600 file loaded via --env-file.

    The engine CLI parses the file on the node where the wrapped command
    runs, so this is the host-side cache path. StdoutHost's no-op write and
    rc-0 run keep the retry path referencing the original submission's file.
    """
    tracking_dir = f"{cache_host}/tracking"
    host.mkdir(tracking_dir)
    final = f"{tracking_dir}/env"
    tmp = f"{tracking_dir}/env.tmp.{os.getpid()}"
    host.write_file(tmp, "".join(f"{key}={env[key]}\n" for key in sorted(env)))
    chmod = host.run(["chmod", "600", tmp], check=False)
    if chmod.returncode != 0:
        raise RuntimeError(f"chmod 600 failed for {tmp}")
    move = host.run(["mv", "-f", tmp, final], check=False)
    if move.returncode != 0:
        raise RuntimeError(f"mv -f {tmp} to {final} failed")
    return final


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


def make_adapter(config: BackendConfig, *, host, project_name: str = ""):
    backend_type = config.shared.type
    if backend_type == "slurm":
        from jernerics.backend.slurm.adapter import SlurmAdapter

        return SlurmAdapter.from_config(config, host=host, project_name=project_name)
    elif backend_type == "pueue":
        from jernerics.backend.pueue.adapter import PueueAdapter

        return PueueAdapter.from_config(config, host=host, project_name=project_name)
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
    adapter = make_adapter(config, host=host, project_name=project_name)

    shared = config.shared
    remote_dir = substitute_project_name(
        shared.remote_dir.replace("~", host.home), project_name
    )
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
    if not artifact_env_resolved:
        env_file = None
    elif dry_run:
        env_file = f"{cache_host}/tracking/env"
    else:
        env_file = write_env_file(host, cache_host, artifact_env_resolved)

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
        param_overrides=spec.param_overrides,
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
        multiline=not dry_run,
        retry_ctx_path=retry_ctx_path,
        env_file=env_file,
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

    result = infra.adapter.submit_sweep(params)
    if spec.project_name and result is not None:
        events = build_submission_events(spec, backend_name, result)
        write_submission_events(
            events,
            host,
            f"{cache_host}/tracking/{spec.study_name}",
            f"{spec.submission_id}.jsonl",
        )
    return result
