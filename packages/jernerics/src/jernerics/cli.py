import json
import re
import shlex
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import tomli_w
import tomllib
import typer
from rich.console import Console
from rich.table import Table

from .backend.components.container import Apptainer
from .backend.components.host import SSHHost
from .backend.components.project_sync import FileSyncer
from .backend.local_backend import LocalBackend
from .backend.models import SweepSpec
from .backend.slurm_backend import SlurmBackend
from .config import (
    BackendConfig,
    ConfigNotFound,
    ExitCode,
    _normalize_time,
    find_pyproject_dir,
    get_project_name,
    load_backend_config,
    load_config,
    load_tracking_server,
)
from .container.templates import generate_container_def, list_templates

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")


SAFE_RELPATH = re.compile(r"^[a-zA-Z0-9_./\-]+$")


def _validate_relpath(path: str, desc: str) -> str:
    if not SAFE_RELPATH.match(path):
        raise SystemExit(
            f"Error: {desc} path '{path}' contains unsafe characters. "
            "Only alphanumeric, underscore, hyphen, period, and slash allowed."
        )
    if ".." in path:
        raise SystemExit(
            f"Error: {desc} path '{path}' must not contain '..' (path traversal)."
        )
    return path


def _resolve_remote_dir(config: BackendConfig, project_name: str) -> str:
    remote_dir = config.remote_dir.replace("{project_name}", project_name)
    return remote_dir.replace("{project-name}", project_name)


def _resolve_cache_host(config: BackendConfig, project_name: str) -> str:
    if config.cache_dir:
        cache = config.cache_dir.replace("{project_name}", project_name)
        cache = cache.replace("{project-name}", project_name)
        return cache.replace("~", "$HOME")
    return f"{_resolve_remote_dir(config, project_name)}/.jernerics"


def _build_storage_path(cache_path: str, study_name: str) -> str:
    return f"/{cache_path}/optuna/{study_name}.journal"


def _save_job_meta(
    project_dir: Path,
    job_id: str,
    output_pattern: str,
    error_pattern: str,
    remote_dir: str,
    n_trials: int,
) -> None:
    job_meta = {
        "job_id": job_id,
        "output_pattern": output_pattern,
        "error_pattern": error_pattern,
        "remote_dir": remote_dir,
        "n_trials": n_trials,
    }
    meta_dir = project_dir / ".jernerics" / "jobs"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_file = meta_dir / f"{job_id}.json"
    meta_file.write_text(json.dumps(job_meta, indent=2))


def _get_backend(backend_name: str) -> tuple[SlurmBackend, str, Path]:
    """Load a backend by name. Returns (backend, project_name, project_dir)."""
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        config = load_backend_config(backend_name, project_dir)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    if not config.host:
        print(
            "Error: No host configured for backend "
            f"'{backend_name}'.\n"
            f"  Add host to [tool.jernerics.backends.{backend_name}] "
            "in pyproject.toml"
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    project_name = get_project_name(project_dir)
    remote_dir = _resolve_remote_dir(config, project_name)
    tracking_server = load_tracking_server(project_dir)

    host = SSHHost(config.host)
    container = Apptainer(host)
    syncer = FileSyncer(host, remote_dir)

    backend = SlurmBackend(
        host=host,
        container=container,
        syncer=syncer,
        remote_dir=remote_dir,
        partition=config.partition,
        time=config.time,
        mem=config.mem,
        cpus=config.cpus,
        max_concurrent_jobs=config.max_concurrent_jobs,
        cache_dir=config.cache_dir,
        tracking_server=tracking_server,
        heartbeat_interval_s=config.heartbeat_interval_s,
    )

    return backend, project_name, project_dir


# ── run local ────────────────────────────────────────────────────────────────


@app.command("local")
def run_local(
    dag_file: Annotated[str, typer.Argument(help="Path to the DAG file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
):
    dag_path = Path(dag_file).resolve()
    config_path = Path(config_file).resolve()

    if not dag_path.exists():
        print(f"Error: DAG file not found: {dag_path}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        sweep = load_config(str(config_path))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_dir = find_pyproject_dir()
    project_name = get_project_name(project_dir) if project_dir else None
    tracking_server = load_tracking_server(project_dir) if project_dir else None

    from .paths import cache_dir

    project_cache = cache_dir()
    optuna_dir = project_cache / "optuna"
    optuna_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"local_{config_path.stem}_{timestamp}"
    storage_path = str(optuna_dir / (study_name + ".journal"))

    spec = SweepSpec(
        dag_path=dag_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_path,
        n_trials=sweep.n_trials,
        project_name=project_name,
        server_addr=tracking_server,
    )

    backend = LocalBackend(tracking_server=tracking_server)

    try:
        backend.submit_sweep(spec, direction=sweep.direction)
    except RuntimeError:
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── run (remote) ─────────────────────────────────────────────────────────────


@app.command("run")
def run_remote(
    dag_file: Annotated[str, typer.Argument(help="Path to the DAG file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    set_opt: Annotated[
        list[str] | None,
        typer.Option("--set", "-S", help="Set SLURM option (key=value)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without submitting"),
    ] = False,
):
    if set_opt is None:
        set_opt = []
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    dag_path = Path(dag_file).resolve()
    config_path = Path(config_file).resolve()

    if not dag_path.exists():
        print(f"Error: DAG file not found: {dag_path}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        sweep = load_config(str(config_path))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    cli_overrides = {}
    for opt in set_opt:
        if "=" not in opt:
            print(f"Error: Invalid --set option: {opt}. Expected format: key=value")
            raise SystemExit(ExitCode.CONFIG_ERROR)
        key, value = opt.split("=", 1)
        if not key:
            print(f"Error: Empty key in --set option: {opt}")
            raise SystemExit(ExitCode.CONFIG_ERROR)
        cli_overrides[key] = value

    backend, project_name, project_dir = _get_backend(backend_name)

    slurm_opts = {
        "partition": backend.partition,
        "time": backend.time,
        "mem": backend.mem,
        **{k: _normalize_time(v) if k == "time" else v for k, v in sweep.slurm.items()},
        **{
            k: _normalize_time(v) if k == "time" else v
            for k, v in cli_overrides.items()
        },
    }
    slurm_opts = {k: v for k, v in slurm_opts.items() if v is not None}

    max_parallel = slurm_opts.pop("max_parallel", backend.max_concurrent_jobs)
    try:
        max_parallel_val = int(max_parallel) if max_parallel else 0
    except (ValueError, TypeError) as e:
        print(f"Error: max_parallel must be an integer, got: {max_parallel!r}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from e

    n_trials = sweep.n_trials

    dag_relpath = _validate_relpath(str(dag_path.relative_to(project_dir)), "DAG file")
    config_relpath = _validate_relpath(
        str(config_path.relative_to(project_dir)), "Config file"
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"{project_name}_{config_path.stem}_{timestamp}"
    storage_url = _build_storage_path("cache", study_name)

    spec = SweepSpec(
        dag_path=dag_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_url,
        n_trials=n_trials,
        dag_relpath=dag_relpath,
        config_relpath=config_relpath,
        project_name=project_name,
        server_addr=backend.tracking_server,
        max_parallel=max_parallel_val or None,
        slurm_overrides=slurm_opts,
    )

    cache_host = _resolve_cache_host(
        load_backend_config(backend_name, project_dir), project_name
    )
    output_pattern = slurm_opts.get("output", f"{cache_host}/logs/%A_%a.out")
    error_pattern = slurm_opts.get("error", f"{cache_host}/logs/%A_%a.err")

    if dry_run:
        print("=== DRY RUN ===")
        print(f"Backend: {backend_name}")
        print(f"Host: {backend.host.host}")
        print(f"Remote dir: {backend.remote_dir}")
        print()
        print("=== SLURM SCRIPT ===")
        print(
            backend._generate_sweep_script(
                setup_command=backend._build_setup_command(
                    study_name=study_name,
                    storage_path=storage_url,
                    direction=sweep.direction,
                ),
                trial_command=backend._build_trial_command(
                    dag_relpath=dag_relpath,
                    config_relpath=config_relpath,
                    study_name=study_name,
                    storage_path=storage_url,
                    project_name=project_name,
                    tracking_dir=f"/cache/tracking/{study_name}",
                    tracking_server=backend.tracking_server,
                ),
                array_spec=f"1-{n_trials}"
                + (f"%{max_parallel_val}" if max_parallel_val > 0 else ""),
                study_name=study_name,
                project_name=project_name,
                slurm_overrides=slurm_opts,
            )
        )
        return

    print(f"[1/4] Syncing project to {backend.host.host}:{backend.remote_dir}...")
    backend.syncer.sync_project(project_dir)

    print("[2/4] Ensuring cache directory exists...")
    if backend.cache_dir:
        cache_host_path = _resolve_cache_host(
            load_backend_config(backend_name, project_dir), project_name
        )
        backend.host.mkdir(f"{cache_host_path}/optuna")
    else:
        backend.host.mkdir(f"{backend.remote_dir}/.jernerics/optuna")
        backend.host.mkdir(f"{backend.remote_dir}/.jernerics/logs")
        print("[3/4] (Using remote_dir/.jernerics as cache)")

    if not backend.syncer.container_exists():
        print(
            "Error: container.sif not found on remote.\n"
            "  Run 'jernerics build --backend <name>' first."
        )
        raise SystemExit(ExitCode.CONTAINER_ERROR)

    print("[4/4] Submitting job...")
    try:
        job_id = backend.submit_sweep(spec, direction=sweep.direction)

        _save_job_meta(
            project_dir=project_dir,
            job_id=job_id,
            output_pattern=str(output_pattern),
            error_pattern=str(error_pattern),
            remote_dir=backend.remote_dir,
            n_trials=n_trials,
        )

        print(f"\nJob submitted: {job_id}")
        print("\nMonitor progress:")
        print(f"  jernerics logs --backend {backend_name} {job_id} --follow")
    except RuntimeError as e:
        print(f"Error: Failed to submit job: {e}")
        raise SystemExit(ExitCode.SLURM_ERROR) from None


# ── build ────────────────────────────────────────────────────────────────────


@app.command("build")
def build(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    project_dir: Annotated[
        str, typer.Argument(help="Project directory (default: current)")
    ] = ".",
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force rebuild even if up to date"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview actions without executing"),
    ] = False,
):
    project_path = Path(project_dir).resolve()
    if not project_path.exists():
        print(f"Error: Project directory not found: {project_path}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    backend, _, _ = _get_backend(backend_name)

    lock_path = project_path / "uv.lock"
    if not lock_path.exists():
        print("Error: uv.lock not found. Run 'uv lock' first.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    container_def_path = project_path / "container.def"
    if not container_def_path.exists():
        container_def_path.write_text(generate_container_def("python"))
        print("Created: container.def")

    if (
        not dry_run
        and not force
        and not backend.syncer.container_needs_rebuild(lock_path)
    ):
        print("Container is up to date. Use --force to rebuild.")
        return

    if dry_run:
        print("=== DRY RUN ===")
        print(f"Project dir: {project_path}")
        print(f"Remote dir: {backend.remote_dir}")
        print(f"Host: {backend.host.host}")
        print()
        print("Would sync files and submit build job.")
        return

    print(f"[1/3] Syncing project to {backend.host.host}:{backend.remote_dir}")
    backend.syncer.sync_project(project_path)

    print("[2/3] Creating logs directory...")
    backend.host.mkdir(f"{backend._cache_path()}/logs")

    print("[3/3] Submitting build job...")
    try:
        job_id = backend.submit_build_job()

        _save_job_meta(
            project_dir=project_path,
            job_id=job_id,
            output_pattern=f"{backend._cache_path()}/logs/build_%j.out",
            error_pattern=f"{backend._cache_path()}/logs/build_%j.err",
            remote_dir=backend.remote_dir,
            n_trials=1,
        )

        print(f"\nBuild job submitted: {job_id}")
        print("\nMonitor progress:")
        print(f"  jernerics logs --backend {backend_name} {job_id} --follow")
    except RuntimeError as e:
        print(f"Error: Failed to submit build job: {e}")
        raise SystemExit(ExitCode.SLURM_ERROR) from None


# ── jobs ─────────────────────────────────────────────────────────────────────


@app.command("jobs")
def jobs(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include completed jobs"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    backend, _, _ = _get_backend(backend_name)
    job_list = backend.list_jobs(include_completed=all)

    if json_output:
        data = [
            {
                "job_id": job.job_id,
                "name": job.name,
                "status": job.status,
            }
            for job in job_list
        ]
        print(json.dumps(data, indent=2))
        return

    if not job_list:
        print("No jobs found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("JOB_ID")
    table.add_column("NAME", max_width=20)
    table.add_column("STATUS")

    for job in job_list:
        table.add_row(job.job_id, job.name, job.status)

    Console().print(table)


# ── cancel ───────────────────────────────────────────────────────────────────


@app.command("cancel")
def cancel(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    job_id: Annotated[
        str | None,
        typer.Argument(help="Job ID to cancel"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Cancel all your jobs"),
    ] = False,
):
    backend, _, _ = _get_backend(backend_name)

    if all:
        if backend.cancel_all():
            print("Cancelled all jobs.")
        else:
            print("Failed to cancel jobs.")
        return

    if job_id is None:
        print("Error: Specify a job ID or use --all")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    if backend.cancel(job_id):
        print(f"Cancelled job {job_id}.")
    else:
        print(f"Failed to cancel job {job_id}.")


# ── logs ─────────────────────────────────────────────────────────────────────


@app.command("logs")
def logs(
    job_id: Annotated[str, typer.Argument(help="Job ID")],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Follow log output (tail -f)"),
    ] = False,
    array_index: Annotated[
        int | None,
        typer.Option("--array-index", "-i", help="Array task index (for array jobs)"),
    ] = None,
    stderr: Annotated[
        bool,
        typer.Option("--stderr", "-e", help="Show stderr instead of stdout"),
    ] = False,
):
    backend, _, project_dir = _get_backend(backend_name)

    meta_file = project_dir / ".jernerics" / "jobs" / f"{job_id}.json"
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        output_pattern = meta.get("output_pattern", "logs/slurm_%j.out")
        error_pattern = meta.get("error_pattern", "logs/slurm_%j.err")
        meta_remote_dir = meta.get("remote_dir", backend.remote_dir)
        n_trials = meta.get("n_trials", 1)
    else:
        cache_host = _resolve_cache_host(
            load_backend_config(backend_name, project_dir),
            get_project_name(project_dir),
        )
        output_pattern = f"{cache_host}/logs/%A_%a.out"
        error_pattern = f"{cache_host}/logs/%A_%a.err"
        meta_remote_dir = backend.remote_dir
        n_trials = 1

    log_pattern = error_pattern if stderr else output_pattern

    base_job_id = job_id.split("_")[0] if "_" in job_id else job_id
    array_idx = job_id.split("_")[1] if "_" in job_id else None

    effective_array_index = array_index if array_index is not None else array_idx
    if effective_array_index is None and n_trials == 1:
        effective_array_index = 1

    log_file = backend.resolve_log_path(
        log_pattern,
        job_id=job_id,
        array_task_id=effective_array_index,
        replace_unknown_with_wildcard=True,
    )

    if not log_file.startswith("/") and not log_file.startswith("~"):
        log_file = f"{meta_remote_dir}/{log_file}"

    max_retries = 5
    retry_delay = 1.0

    if "*" in log_file:
        is_array_pattern = "%a" in log_pattern and effective_array_index is None
        if follow and is_array_pattern:
            print("Error: --follow requires --array-index for array jobs")
            raise SystemExit(ExitCode.GENERAL_ERROR)
        for attempt in range(max_retries):
            result = backend.host.run(
                [f"cat {log_file}"], check=False, capture_output=True, text=True
            )
            if result.returncode == 0:
                print(result.stdout)
                return
            if attempt == 0:
                print("Waiting for logs...")
            time.sleep(retry_delay)
        print(f"Error: Log files not found: {log_file}")
        raise SystemExit(ExitCode.GENERAL_ERROR)
    elif follow:
        for attempt in range(max_retries):
            result = backend.host.run([f"test -f {log_file}"], check=False)
            if result.returncode == 0:
                break
            if attempt == 0:
                print("Waiting for logs...")
            time.sleep(retry_delay)
        else:
            print(f"Error: Log file not found: {log_file}")
            raise SystemExit(ExitCode.GENERAL_ERROR)
        subprocess.run(["ssh", backend.host.host, "tail", "-f", log_file], check=False)
    else:
        for attempt in range(max_retries):
            result = backend.host.run(
                [f"cat {log_file}"], check=False, capture_output=True, text=True
            )
            if result.returncode == 0:
                print(result.stdout)
                return
            if attempt == 0:
                print("Waiting for logs...")
            time.sleep(retry_delay)
        print(f"Error: Log file not found: {log_file}")
        raise SystemExit(ExitCode.GENERAL_ERROR)


# ── clean ────────────────────────────────────────────────────────────────────


@app.command("clean")
def clean(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    results: Annotated[
        bool,
        typer.Option("--results", help="Delete results/ directory"),
    ] = False,
    logs: Annotated[
        bool,
        typer.Option("--logs", help="Delete logs"),
    ] = False,
    container: Annotated[
        bool,
        typer.Option("--container", help="Delete container.sif"),
    ] = False,
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Delete everything"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Actually delete (dry-run by default)"),
    ] = False,
):
    backend, project_name, project_dir = _get_backend(backend_name)
    cache_host = _resolve_cache_host(
        load_backend_config(backend_name, project_dir), project_name
    )

    to_delete = []
    if all:
        to_delete = ["results/", f"{cache_host}/logs/", "container.sif"]
    else:
        if results:
            to_delete.append("results/")
        if logs:
            to_delete.append(f"{cache_host}/logs/")
        if container:
            to_delete.append("container.sif")

    if not to_delete:
        print(
            "Error: Nothing to clean. Specify --results, --logs, --container, or --all"
        )
        raise SystemExit(ExitCode.GENERAL_ERROR)

    print(f"Would delete from {backend.host.host}:{backend.remote_dir}:")
    for item in to_delete:
        print(f"  - {item}")

    if not force:
        print("\nDry run. Use --force to actually delete.")
        return

    for item in to_delete:
        path = f"{backend.remote_dir}/{item}"
        result = backend.host.run([f"rm -rf {path}"], check=False)
        if result.returncode != 0:
            print(f"Failed to delete {item}: {result.stderr}")
        else:
            print(f"Deleted: {item}")


# ── sync ─────────────────────────────────────────────────────────────────────


@app.command("sync")
def sync(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    study: Annotated[
        str | None,
        typer.Option("--study", "-s", help="Scope to a single study"),
    ] = None,
):
    backend, _, project_dir = _get_backend(backend_name)

    if not backend.tracking_server:
        print("Error: No tracking server configured.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    project_name = get_project_name(project_dir)
    cache_host = _resolve_cache_host(
        load_backend_config(backend_name, project_dir), project_name
    )

    bind_args = f'"{backend.remote_dir}:/work" "{cache_host}:/cache"'

    inner_cmd = (
        f"python -m jernerics.tracking.replay_runner"
        f" --tracking-dir /cache/tracking"
        f" --server-addr {backend.tracking_server}"
    )
    if study:
        inner_cmd += f" --study {shlex.quote(study)}"

    cmd = (
        f"cd {backend.remote_dir} && "
        f"apptainer exec --fakeroot --contain --nv"
        f" --pwd /work --bind {bind_args}"
        f" container.sif {inner_cmd}"
    )

    print(f"Syncing tracking data from {backend.host.host}...")
    result = backend.host.run([cmd], check=False, capture_output=True, text=True)
    print(result.stdout)
    if result.returncode != 0:
        print(f"Sync failed: {result.stderr}")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    print("Sync complete.")


# ── init ─────────────────────────────────────────────────────────────────────


@app.command("init")
def init(
    project_dir: Annotated[
        str, typer.Argument(help="Directory to initialize (default: current)")
    ] = ".",
    template: Annotated[
        str, typer.Option("--template", "-t", help="Container template to use")
    ] = "python",
    force: Annotated[
        bool,
        typer.Option(
            "--force", "-f", help="Overwrite existing [tool.jernerics] config"
        ),
    ] = False,
):
    if shutil.which("uv") is None:
        print("Error: 'uv' command not found. Please install uv first.")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    project_path = Path(project_dir).resolve()
    project_name = project_path.name

    if template not in list_templates():
        print(
            f"Error: Unknown template: {template}. "
            f"Available: {', '.join(list_templates())}"
        )
        raise SystemExit(ExitCode.GENERAL_ERROR)

    project_path.mkdir(parents=True, exist_ok=True)

    pyproject_path = project_path / "pyproject.toml"
    jernerics_config = _get_default_jernerics_config(project_name)

    if pyproject_path.exists():
        try:
            with open(pyproject_path, "rb") as f:
                existing = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            print(f"Error: Malformed pyproject.toml: {e}")
            raise SystemExit(ExitCode.CONFIG_ERROR) from None

        has_jernerics = "jernerics" in existing.get("tool", {})

        if (
            has_jernerics
            and not force
            and not typer.confirm(
                "[tool.jernerics] already exists in pyproject.toml. Overwrite?",
                default=False,
            )
        ):
            print("Skipped updating pyproject.toml")
            return

        existing.setdefault("tool", {})["jernerics"] = jernerics_config
        merged = existing
    else:
        merged = _create_minimal_pyproject(project_name, jernerics_config)

    with open(pyproject_path, "wb") as f:
        tomli_w.dump(merged, f)

    print("Updated: pyproject.toml")

    container_def_path = project_path / "container.def"
    if not container_def_path.exists():
        container_def_path.write_text(generate_container_def(template))
        print("Created: container.def")
    else:
        print("Skipped: container.def (already exists)")

    src_dir = project_path / "src"
    if not src_dir.exists():
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "__init__.py").write_text('"""Project source."""\n')
        print("Created: src/")
    else:
        print("Skipped: src/ (already exists)")

    uv_result = subprocess.run(
        ["uv", "sync"],
        cwd=project_path,
        capture_output=True,
        check=False,
        text=True,
    )
    if uv_result.returncode == 0:
        print("Generated: uv.lock")
    else:
        print(f"Warning: 'uv sync' failed: {uv_result.stderr.strip()}")

    print(f"\nProject initialized in {project_path}")
    print("\nNext steps:")
    print("  1. Edit pyproject.toml to add dependencies")
    print(
        "  2. Create your DAG and config files "
        "(e.g., experiments/dag.py, configs/default.py)"
    )
    print("  3. Run 'jernerics build --backend <name>' to build on remote")


def _get_default_jernerics_config(project_name: str) -> dict:
    return {
        "backends": {
            "hpc": {
                "type": "slurm",
                "host": "your-username@hpc.example.edu",
                "remote_dir": f"~/experiments/{project_name}",
                "cache_dir": "/scratch/$USER/jernerics",
                "partition": "priority",
                "time": "1:00:00",
                "mem": "16G",
                "cpus": 4,
            }
        }
    }


def _create_minimal_pyproject(project_name: str, jernerics_config: dict) -> dict:
    return {
        "project": {
            "name": project_name,
            "version": "0.1.0",
            "description": "Add description here",
            "requires-python": ">=3.12",
            "dependencies": ["jernerics"],
        },
        "tool": {
            "uv": {
                "sources": {
                    "jernerics": {"git": "https://github.com/jerrydzhang/jernerics.git"}
                }
            },
            "jernerics": jernerics_config,
        },
        "build-system": {
            "requires": ["hatchling"],
            "build-backend": "hatchling.build",
        },
    }


def main():
    app()
