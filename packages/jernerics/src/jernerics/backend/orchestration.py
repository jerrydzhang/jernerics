import shlex
from collections.abc import Callable
from pathlib import Path

from jernerics.backend.components.build_marker import needs_rebuild
from jernerics.backend.components.path_resolver import PathResolver
from jernerics.backend.models import JobInfo, SubmitResult, SweepSubmission
from jernerics.retry import RetryContext


def _compose_build_script(
    build_command: list[str],
    remote_dir: str,
    marker_path: str,
    *,
    build_dir: str | None = None,
) -> str:
    cmd_str = " ".join(shlex.quote(c) for c in build_command)

    if build_dir is not None:
        return (
            f"set -e\n"
            f"mkdir -p {build_dir}\n"
            f"export APPTAINER_TMPDIR={build_dir}\n"
            f"cd {remote_dir}\n"
            f"{cmd_str}\n"
            f"rm -rf {build_dir}\n"
            f"mkdir -p {Path(marker_path).parent}\n"
            f"touch {marker_path}\n"
        )

    return (
        f"set -e\n"
        f"cd {remote_dir}\n"
        f"{cmd_str}\n"
        f"mkdir -p {Path(marker_path).parent}\n"
        f"touch {marker_path}\n"
    )


def submit_build(
    *,
    host,
    container,
    syncer,
    paths: PathResolver,
    remote_dir: str,
    project_dir: Path,
    project_name: str,
    force: bool = False,
    dry_run: bool = False,
    generate_submit_job,
) -> str:
    """Shared build orchestration. Syncs, composes build script, submits job.

    Returns the submitted job ID.
    """
    lock_path = project_dir / "uv.lock"
    if not lock_path.exists():
        raise FileNotFoundError("uv.lock not found. Run 'uv lock' first.")

    container_def_path = project_dir / "container.def"
    dockerfile_path = project_dir / "Dockerfile"
    has_build_file = container_def_path.exists() or dockerfile_path.exists()

    if not has_build_file:
        from jernerics.container.starters import generate_container_def

        container_def_path.write_text(generate_container_def("python"))
        print("Created: container.def")

    cache_host = paths.resolve_cache(project_name)
    marker_path = f"{cache_host}/.build_marker"

    if not dry_run and not force and not needs_rebuild(host, marker_path, lock_path):
        print("Container is up to date. Use --force to rebuild.")
        return ""

    host_label = getattr(host, "host", None)
    if dry_run:
        print("=== DRY RUN ===")
        print(f"Project dir: {project_dir}")
        print(f"Remote dir: {remote_dir}")
        if host_label:
            print(f"Host: {host_label}")
        print()
        print("Would sync files and submit build job.")
        return ""

    if syncer is not None:
        label = host_label or "local"
        print(f"Syncing project to {label}:{remote_dir}...")
        syncer.sync_project(project_dir)

    build_script = _compose_build_script(
        build_command=container.build_command(remote_dir),
        remote_dir=remote_dir,
        marker_path=marker_path,
        build_dir=paths.resolve_build_dir(project_name),
    )

    submission_script = generate_submit_job(
        build_script, name="container-build", log_dir=f"{cache_host}/logs"
    )

    result = host.run(
        ["bash"],
        input=submission_script,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to submit build job: {result.stderr.strip()}")

    job_id = result.stdout.strip()
    print(f"Build job submitted: {job_id}")
    return job_id


def sync(
    *,
    host,
    container,
    paths: PathResolver,
    remote_dir: str,
    project_name: str,
    tracking_server: str | None,
    study: str | None = None,
) -> None:
    if not tracking_server:
        raise RuntimeError("No tracking server configured")

    import shlex

    cache_host = paths.resolve_cache(project_name)
    bind_args = paths.bind_args(cache_host)

    inner_cmd = (
        "python -m jernerics.tracking.replay_runner"
        " --tracking-dir /cache/tracking"
        f" --server-addr {tracking_server}"
    )
    if study:
        inner_cmd += f" --study {shlex.quote(study)}"

    wrapped = container.wrap(inner_cmd, bind_args)
    cmd = f"cd {remote_dir} && {wrapped}"

    host_desc = getattr(host, "host", "local")
    print(f"Syncing tracking data from {host_desc}...")
    result = host.run([cmd], check=False, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"Sync failed: {result.stderr}")
        raise RuntimeError(f"Sync failed: {result.stderr}")
    print("Sync complete.")


def clean(
    *,
    host,
    paths: PathResolver,
    remote_dir: str,
    project_name: str,
    full: bool = False,
    force: bool = False,
    list_active_jobs: Callable[[], list[JobInfo]],
    scheduler_cleanup: Callable[[], None],
) -> None:
    """Shared clean orchestration."""
    cache_host = paths.resolve_cache(project_name)

    target_desc = "cache + project directory" if full else "cache directory"
    host_label = getattr(host, "host", None)
    if host_label:
        print(f"Target: {target_desc} on {host_label}")
    else:
        print(f"Target: {target_desc}")
    print(f"  cache:   {cache_host}")
    if full:
        print(f"  project: {remote_dir}")

    active = list_active_jobs()
    if active:
        print(f"\nError: {len(active)} active job(s) found. Cancel them first.")
        for j in active:
            print(f"  {j.job_id}  {j.name}  {j.status}")
        raise RuntimeError("Active jobs prevent cleaning")

    result = host.run(
        [f"find {cache_host}/tracking -name '*.pb' 2>/dev/null | head -n 1"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        print("\nError: Unsynced tracking data found. Run sync first.")
        raise RuntimeError("Unsynced tracking data")

    r = host.run(["test", "-d", cache_host], check=False, capture_output=True)
    if r.returncode != 0:
        print(f"\nError: cache directory '{cache_host}' not found.")
        raise FileNotFoundError(f"Cache directory not found: {cache_host}")

    if full:
        r = host.run(["test", "-d", remote_dir], check=False, capture_output=True)
        if r.returncode != 0:
            print(f"\nError: project directory '{remote_dir}' not found.")
            raise FileNotFoundError(f"Project directory not found: {remote_dir}")

    if not force:
        print("\nDry run. Use --force to execute.")
        return

    scheduler_cleanup()

    r = host.run(["rm", "-rf", cache_host], check=False, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"Failed to delete {cache_host}: {r.stderr}")
        raise RuntimeError(f"Failed to delete {cache_host}")
    print(f"Deleted: {cache_host}")

    if full:
        r = host.run(
            ["rm", "-rf", remote_dir],
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            print(f"Failed to delete {remote_dir}: {r.stderr}")
            raise RuntimeError(f"Failed to delete {remote_dir}")
        print(f"Deleted: {remote_dir}")


def prepare_and_submit(
    *,
    host,
    container,
    syncer,
    paths: PathResolver,
    remote_dir: str,
    spec: SweepSubmission,
    project_dir: Path,
    project_name: str,
    direction: str,
    backend_name: str,
    auto_retry: bool,
    local_cache_dir: Path | None,
    cli_overrides: dict[str, str] | None,
    ensure_submission_ready: Callable[[], None],
    submit_sweep: Callable[..., SubmitResult],
    save_meta: Callable[[SubmitResult], None],
) -> SubmitResult:
    """Shared prepare-and-submit orchestration.

    Handles sync, readiness checks, retry context construction,
    submission, and job meta saving.
    """
    if syncer is not None:
        host_label = getattr(host, "host", "local")
        print(f"Syncing project to {host_label}:{remote_dir}...")
        syncer.sync_project(project_dir)

    ensure_submission_ready()

    cache_host = paths.resolve_cache(project_name)
    retry_ctx = None
    if auto_retry and local_cache_dir is not None:
        retry_dir_host = f"{cache_host}/retry"
        host.mkdir(retry_dir_host)
        retry_ctx = RetryContext(
            study_name=spec.study_name,
            backend_name=backend_name,
            dag_relpath=spec.dag_relpath,
            config_relpath=spec.config_relpath,
            cli_overrides=cli_overrides or {},
            storage_path=paths.expand_storage_url(spec.storage_url),
            tracking_dir=paths.tracking_dir(spec.study_name),
            project_dir=paths.work_prefix,
            ctx_path=paths.retry_ctx_path(spec.study_name),
            chain_depth=0,
            project_name=project_name,
        )
        host_ctx_path = paths.retry_host_path(cache_host, spec.study_name)
        host.write_file(host_ctx_path, retry_ctx.to_json())

    if retry_ctx is not None:
        result = submit_sweep(spec, direction=direction, retry_ctx=retry_ctx)
    else:
        result = submit_sweep(spec, direction=direction)

    save_meta(result)
    return result
