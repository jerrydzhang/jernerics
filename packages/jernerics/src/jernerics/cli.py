import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import tomli_w
import tomllib
import typer
from rich.console import Console
from rich.table import Table

from .backend.backend import Backend
from .backend.host import LocalHost, SSHHost
from .backend.local_backend import LocalBackend
from .backend.models import SweepSubmission
from .backend.project_sync import ProjectSync
from .backend.slurm.interactive import InteractiveSession, format_interactive_script
from .config import (
    ConfigNotFound,
    ExitCode,
    InteractiveConfig,
    SlurmConfig,
    find_pyproject_dir,
    get_project_name,
    load_backend_config,
    load_config,
    load_tracking_server,
)
from .container.templates import get_starter, list_starters
from .observability import (
    RemoteStore,
    get_all_runs,
    get_metric_keys,
    get_metric_series,
    get_run_diff,
    get_run_summary,
    render_diff,
    render_runs,
    render_summary,
    render_trace,
    run_exists,
)
from .paths import cache_dir
from .tracking.batch_sync import discover_jsonl_files, replay_tracking
from .tracking.jsonl_io import TrackingReader

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


def _capture_git_hash(cwd: Path | None) -> str | None:
    """Best-effort git commit hash for sweep provenance; None if not a git repo."""
    if cwd is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return None
    return result.stdout.strip() or None


def _get_backend(backend_name: str) -> tuple[Backend, str, Path]:
    """Load a backend by name. Returns (backend, project_name, project_dir)."""
    from .backend.factory import make_backend

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


# ── run local ────────────────────────────────────────────────────────────────


@app.command("local")
def run_local(
    trial_file: Annotated[str, typer.Argument(help="Path to the trial file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
):
    trial_path = Path(trial_file).resolve()
    config_path = Path(config_file).resolve()

    if not trial_path.exists():
        print(f"Error: trial file not found: {trial_path}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        sweep = load_config(str(config_path))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_dir = find_pyproject_dir()
    project_name = get_project_name(project_dir) if project_dir else None
    tracking_server = load_tracking_server() if project_dir else None
    git_hash = _capture_git_hash(project_dir or trial_path.parent)

    project_cache = cache_dir()
    optuna_dir = project_cache / "optuna"
    optuna_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"local_{config_path.stem}_{timestamp}"
    storage_path = str(optuna_dir / (study_name + ".journal"))

    spec = SweepSubmission(
        trial_path=trial_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_path,
        n_trials=sweep.n_trials,
        project_name=project_name,
        server_addr=tracking_server,
        grid=sweep.grid,
        git_hash=git_hash,
    )

    backend = LocalBackend(tracking_server=tracking_server)

    try:
        backend.submit_sweep(spec, direction=sweep.direction)
    except RuntimeError:
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── run (remote) ─────────────────────────────────────────────────────────────


@app.command("run")
def run_remote(
    trial_file: Annotated[str, typer.Argument(help="Path to the trial file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    set_opt: Annotated[
        list[str] | None,
        typer.Option("--set", "-S", help="Set backend option (key=value)"),
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

    trial_path = Path(trial_file).resolve()
    config_path = Path(config_file).resolve()

    if not trial_path.exists():
        print(f"Error: trial file not found: {trial_path}")
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

    trial_relpath = _validate_relpath(
        str(trial_path.relative_to(project_dir)), "trial file"
    )
    config_relpath = _validate_relpath(
        str(config_path.relative_to(project_dir)), "Config file"
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"{project_name}_{config_path.stem}_{timestamp}"
    storage_url = backend.storage_path(study_name)
    git_hash = _capture_git_hash(project_dir)

    spec = SweepSubmission(
        trial_path=trial_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_url,
        n_trials=sweep.n_trials,
        trial_relpath=trial_relpath,
        config_relpath=config_relpath,
        project_name=project_name,
        server_addr=backend.tracking_server,
        grid=sweep.grid,
        git_hash=git_hash,
    )

    try:
        result = backend.prepare_and_submit(
            spec,
            project_dir=project_dir,
            project_name=project_name,
            direction=sweep.direction,
            dry_run=dry_run,
            backend_name=backend_name,
            experiment_overrides=sweep.backend_overrides.get(backend_name, {}),
            cli_overrides=cli_overrides,
            local_cache_dir=cache_dir(),
        )
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if result is not None:
        print("\nMonitor progress:")
        job_id = result.submissions[0].job_id
        print(f"  jernerics logs --backend {backend_name} {job_id} --follow")
        print(f"  jernerics wait --backend {backend_name} {job_id}")

        tracking_server = load_tracking_server()
        if tracking_server:
            query_hint = (
                f"  curl -X POST {tracking_server}/query"
                ' -H "Content-Type: application/json"'
                ' -d \'{"sql": "SELECT * FROM tracked_values'
                " ORDER BY timestamp_ns DESC LIMIT 5\"}'"
            )
            print("\nQuery metrics:")
            print(query_hint)


# ── interactive ──────────────────────────────────────────────────────────────


def _build_interactive_session(
    backend_name: str,
    *,
    time: str | None,
    gpus: int | None,
    partition: str | None,
    constraint: str | None,
) -> InteractiveSession:
    """Resolve config + CLI flags into an :class:`InteractiveSession`."""
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        config = load_backend_config(backend_name)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    if config.shared.type != "slurm" or not isinstance(config.backend, SlurmConfig):
        print(
            f"Error: 'interactive' requires a slurm backend; '{backend_name}'"
            f" is '{config.shared.type}'."
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    if not config.shared.host:
        print("Error: 'interactive' requires an SSH host (none configured).")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    project_name = get_project_name(project_dir)
    host = SSHHost(config.shared.host)
    slurm = config.backend
    interactive = config.interactive or InteractiveConfig()

    remote_dir = (
        config.shared.remote_dir.replace("~", host.home)
        .replace("{project_name}", project_name)
        .replace("{project-name}", project_name)
    )
    cache_host = (
        config.shared.cache_dir.replace("~", host.home)
        .replace("{project_name}", project_name)
        .replace("{project-name}", project_name)
        if config.shared.cache_dir
        else f"{host.home}/.cache/jernerics"
    )

    login_target = config.shared.host
    user = login_target.split("@", 1)[0] if "@" in login_target else None

    return InteractiveSession(
        host=host,
        job_name=f"jernerics-interactive-{project_name}",
        remote_dir=remote_dir,
        container_image=f"{remote_dir}/container.sif",
        cache_host=cache_host,
        partition=partition or interactive.partition or slurm.partition,
        time_limit=time or interactive.time or slurm.time or "4:00:00",
        gpus=gpus if gpus is not None else interactive.gpus,
        mem=interactive.mem or slurm.mem or "16G",
        cpus=interactive.cpus if interactive.cpus is not None else slurm.cpus,
        constraint=constraint or interactive.constraint,
        tmux_session=interactive.tmux_session,
        login_target=login_target,
        user=user,
    )


def _interactive_connect(
    session: InteractiveSession,
    node: str,
    backend_name: str,
    *,
    job_id: str | None = None,
) -> None:
    """Attach to the allocation; print a reconnect/teardown hint on exit."""
    # Sync hook: continuous code sync (mutagen) plugs in here (task uyy.2).
    session.connect(node)
    if job_id is None:
        info = session.find_existing()
        job_id = info.job_id if info else "?"
    print()
    print(f"Disconnected from {node}. The allocation (job {job_id}) is still running.")
    print(f"  Reconnect:  jernerics interactive --backend {backend_name}")
    print(f"  End:        jernerics interactive --backend {backend_name} --end")


@app.command("interactive")
def interactive(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    time: Annotated[
        str | None, typer.Option("--time", help="Walltime, e.g. 4:00:00")
    ] = None,
    gpus: Annotated[
        int | None, typer.Option("--gpus", help="Number of GPUs to allocate")
    ] = None,
    partition: Annotated[
        str | None, typer.Option("--partition", help="SLURM partition")
    ] = None,
    constraint: Annotated[
        str | None, typer.Option("--constraint", help="SLURM constraint, e.g. a100")
    ] = None,
    end: Annotated[
        bool,
        typer.Option("--end", help="Tear down an existing interactive session"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Show sbatch script and ssh command without running"
        ),
    ] = False,
) -> None:
    """Open a reconnectable shell on an allocated GPU node.

    Allocates a GPU via a reservation job that survives SSH disconnect, then
    drops you into a tmux session inside the container. Re-run to reconnect to
    an existing session; use --end to release the allocation.
    """
    session = _build_interactive_session(
        backend_name, time=time, gpus=gpus, partition=partition, constraint=constraint
    )

    if dry_run:
        script = format_interactive_script(
            job_name=session.job_name,
            partition=session.partition,
            time_limit=session.time_limit,
            mem=session.mem,
            cpus=session.cpus,
            gpus=session.gpus,
            constraint=session.constraint,
        )
        print("=== SBATCH SCRIPT ===")
        print(script)
        print()
        print("=== SSH (after allocation; NODE is the compute host) ===")
        print(" ".join(shlex.quote(a) for a in session.ssh_argv("NODE")))
        return

    existing = session.find_existing()

    if end:
        if existing is None:
            print(f"No active interactive session for backend '{backend_name}'.")
            return
        session.end()
        print(f"Cancelled interactive job {existing.job_id} (was {existing.state}).")
        return

    if existing is not None:
        if existing.state == "RUNNING" and existing.node:
            print(f"Reconnecting to job {existing.job_id} on {existing.node}...")
            _interactive_connect(session, existing.node, backend_name)
            return
        print(
            f"Existing session job {existing.job_id} is {existing.state};"
            " waiting for it to start..."
        )
        node = session.wait_for_running(existing.job_id)
        print(f"Allocation running on {node}. Connecting...")
        _interactive_connect(session, node, backend_name)
        return

    readiness = session.host.run(["test", "-f", session.container_image], check=False)
    if readiness.returncode != 0:
        print(f"Error: container not found at {session.container_image}.")
        print("  Run 'jernerics build --backend <name>' first.")
        raise SystemExit(ExitCode.CONTAINER_ERROR) from None

    session.host.mkdir(session.cache_host)
    print(
        f"Submitting interactive allocation ({session.gpus} GPU,"
        f" partition {session.partition}, {session.time_limit})..."
    )
    job_id = session.submit()
    print(f"Submitted job {job_id}. Waiting for it to start...")
    node = session.wait_for_running(job_id)
    print(f"Allocation running on {node}. Connecting...")
    _interactive_connect(session, node, backend_name, job_id=job_id)


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

    backend, project_name, _ = _get_backend(backend_name)

    try:
        backend.build(
            project_path,
            project_name=project_name,
            force=force,
            dry_run=dry_run,
            local_cache_dir=cache_dir(),
        )
    except (RuntimeError, FileNotFoundError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


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
    job_list = backend.list_jobs(include_completed=all, local_cache_dir=cache_dir())

    if json_output:
        data = [
            {
                "job_id": job.job_id,
                "study_name": job.study_name,
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
    table.add_column("STUDY", max_width=30)
    table.add_column("STATUS")

    for job in job_list:
        table.add_row(job.job_id, job.study_name or "—", job.status)

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
        typer.Option("--follow", "-f", help="Follow log output"),
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
    backend, _, _ = _get_backend(backend_name)

    try:
        backend.get_logs(
            job_id,
            follow=follow,
            stderr=stderr,
            local_cache_dir=cache_dir(),
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── wait ─────────────────────────────────────────────────────────────────────


@app.command("wait")
def wait(
    job_id: Annotated[str, typer.Argument(help="Job ID")],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    timeout: Annotated[
        int | None,
        typer.Option(
            "--timeout",
            "-t",
            help="Maximum seconds to wait (omit to wait forever)",
        ),
    ] = None,
    poll_interval: Annotated[
        int,
        typer.Option("--poll-interval", "-p", help="Seconds between status polls"),
    ] = 10,
):
    backend, _, _ = _get_backend(backend_name)

    try:
        success = backend.wait_for_completion(
            job_id, poll_interval=poll_interval, timeout=timeout
        )
    except TimeoutError:
        print(f"Job {job_id} still running after {timeout}s")
        raise SystemExit(2) from None
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if success:
        print(f"Job {job_id} completed successfully")
    else:
        print(f"Job {job_id} finished with non-success status")
        raise SystemExit(1)


# ── clean ────────────────────────────────────────────────────────────────────


@app.command("clean")
def clean(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    full: Annotated[
        bool,
        typer.Option("--full", help="Also delete project source and container"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Execute (dry-run by default)"),
    ] = False,
) -> None:
    backend, project_name, _ = _get_backend(backend_name)

    try:
        backend.clean(project_name, full=full, force=force)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


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
    project_name = get_project_name(project_dir)
    backend.sync(project_name, study=study)


# ── replay ───────────────────────────────────────────────────────────────────


# Server tables that hold one row per ingested tracking event. Counting their
# rows per study approximates "events already synced" — there is no single
# events table; each envelope lands in exactly one of these via insert_event.
_EVENT_TABLES = (
    "tracked_values",
    "params",
    "artifacts",
    "sweep_meta",
    "trial_end",
)


def _count_local_events(tracking_dir: Path, study: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in discover_jsonl_files(tracking_dir, study):
        study_name = path.parent.parent.name
        with TrackingReader(path) as reader:
            counts[study_name] = counts.get(study_name, 0) + sum(1 for _ in reader)
    return counts


def _count_synced_events(store: RemoteStore, study: str) -> int:
    total = 0
    for table in _EVENT_TABLES:
        _, rows = store.query(
            f"SELECT COUNT(*) FROM {table} WHERE study_name = ?", [study]
        )
        total += rows[0][0]
    return total


def _run_dry_run(
    tracking_dir: Path,
    store: RemoteStore,
    study: str | None,
    json_output: bool,
) -> None:
    local_counts = _count_local_events(tracking_dir, study)
    report = []
    for name in sorted(local_counts):
        synced = _count_synced_events(store, name)
        local_n = local_counts[name]
        report.append(
            {
                "study": name,
                "local": local_n,
                "synced": synced,
                "new": max(local_n - synced, 0),
            }
        )

    if json_output:
        print(json.dumps(report, indent=2))
        return

    if not report:
        print("No local events found. Nothing to replay.")
        return

    total_local = sum(r["local"] for r in report)
    total_synced = sum(r["synced"] for r in report)
    total_new = sum(r["new"] for r in report)
    for r in report:
        print(
            f"  {r['study']}: {r['local']} local, {r['synced']} synced, "
            f"{r['new']} would be new"
        )
    print(
        f"\nTotal: {total_local} local, {total_synced} synced, "
        f"{total_new} would be new (dry run — nothing sent)"
    )


@app.command("replay")
def replay(
    study: Annotated[
        str | None,
        typer.Option("--study", "-s", help="Scope replay to a single study"),
    ] = None,
    tracking_dir: Annotated[
        Path | None,
        typer.Option(
            "--tracking-dir",
            help="Tracking directory (default: cache_dir()/tracking)",
        ),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking server base URL"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Compare against server, send nothing"),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    base_url = server or load_tracking_server()
    if not base_url:
        print(
            "Error: No tracking server configured. Set JERNERICS_TRACKING_SERVER "
            "or [tool.jernerics] tracking_server in pyproject.toml."
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    resolved_dir = tracking_dir or (cache_dir() / "tracking")
    api_key = os.environ.get("JERNERICS_API_KEY")

    if dry_run:
        try:
            _run_dry_run(
                resolved_dir, RemoteStore(base_url, api_key=api_key), study, json_output
            )
        except (RuntimeError, httpx.HTTPError) as e:
            print(f"Error: {e}")
            raise SystemExit(ExitCode.GENERAL_ERROR) from None
        return

    result = replay_tracking(
        tracking_dir=resolved_dir,
        base_url=base_url,
        api_key=api_key,
        study=study,
    )
    if json_output:
        print(
            json.dumps(
                {
                    "files_processed": result.files_processed,
                    "events_sent": result.events_sent,
                    "events_failed": result.events_failed,
                    "errors": result.errors,
                }
            )
        )


# ── observability ────────────────────────────────────────────────────────────


def _get_tracking_store() -> tuple[RemoteStore, str]:
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)
    project_name = get_project_name(project_dir)
    server_url = load_tracking_server()
    if not server_url:
        print(
            "Error: No tracking server configured. Set JERNERICS_TRACKING_SERVER "
            "or [tool.jernerics] tracking_server in pyproject.toml."
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)
    api_key = os.environ.get("JERNERICS_API_KEY")
    return RemoteStore(server_url, api_key=api_key), project_name


def _parse_run_id(run_id: str) -> tuple[str, int]:
    if ":" in run_id:
        study, _, tid = run_id.partition(":")
        try:
            trial_id = int(tid)
        except ValueError:
            print(
                f"Error: Invalid trial id in '{run_id}': expected an integer after ':'"
            )
            raise SystemExit(ExitCode.CONFIG_ERROR) from None
        return study, trial_id
    return run_id, 0


@app.command("runs")
def runs(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    store, project = _get_tracking_store()
    try:
        data = get_all_runs(store, project)
    except (RuntimeError, httpx.HTTPError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(data, indent=2))
        return
    render_runs(data, Console())


@app.command("summary")
def summary(
    run_id: Annotated[
        str,
        typer.Argument(help="Run id: study_name or study_name:trial_id"),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    store, project = _get_tracking_store()
    study_name, trial_id = _parse_run_id(run_id)
    try:
        if not run_exists(store, project, study_name, trial_id):
            print(f"Error: Run '{run_id}' not found.")
            raise SystemExit(ExitCode.GENERAL_ERROR)
        data = get_run_summary(store, project, study_name, trial_id)
    except (RuntimeError, httpx.HTTPError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(data, indent=2))
        return
    render_summary(data, Console())


@app.command("diff")
def diff(
    run_a: Annotated[str, typer.Argument(help="First run id (study_name[:trial_id])")],
    run_b: Annotated[str, typer.Argument(help="Second run id (study_name[:trial_id])")],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    store, project = _get_tracking_store()
    try:
        for spec in (run_a, run_b):
            name, tid = _parse_run_id(spec)
            if not run_exists(store, project, name, tid):
                print(f"Error: Run '{spec}' not found.")
                raise SystemExit(ExitCode.GENERAL_ERROR)
        data = get_run_diff(store, project, run_a, run_b)
    except (RuntimeError, httpx.HTTPError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(data, indent=2))
        return
    render_diff(data, Console())


@app.command("trace")
def trace(
    run_id: Annotated[
        str,
        typer.Argument(help="Run id: study_name or study_name:trial_id"),
    ],
    metric: Annotated[
        str | None,
        typer.Argument(help="Metric key to trace"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    store, project = _get_tracking_store()
    study_name, trial_id = _parse_run_id(run_id)
    try:
        if not run_exists(store, project, study_name, trial_id):
            print(f"Error: Run '{run_id}' not found.")
            raise SystemExit(ExitCode.GENERAL_ERROR)

        if metric is None:
            keys = get_metric_keys(store, project, study_name, trial_id)
            if not keys:
                print(f"No metrics found for run '{run_id}'.")
                return
            if json_output:
                print(json.dumps(keys, indent=2))
                return
            print(f"Available metrics for '{run_id}':")
            for entry in keys:
                key = f"{entry['key']:30s}"
                print(f"  {key} ({entry['value_type']}, {entry['count']} points)")
            return

        data = get_metric_series(store, project, study_name, trial_id, metric)
    except (RuntimeError, httpx.HTTPError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if data is None:
        print(f"Error: Metric '{metric}' not found for run '{run_id}'.")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    if json_output:
        print(
            json.dumps(
                {
                    "study_name": study_name,
                    "trial_id": trial_id,
                    "metric": metric,
                    "value_type": data["value_type"],
                    "series": data["series"],
                },
                indent=2,
            )
        )
        return

    label = study_name if trial_id == 0 else f"{study_name}:{trial_id}"
    render_trace(label, metric, data["series"], Console())


def _copy_starter(project_path: Path, starter: str, ext: str, filename: str) -> None:
    target = project_path / filename
    if target.exists():
        print(f"Skipped: {filename} (already exists)")
        return
    try:
        content = get_starter(starter, ext=ext)
        target.write_text(content)
        print(f"Created: {filename}")
    except ValueError:
        pass


_TRIAL_SCAFFOLD = """def trial(config, tracker):
    tracker.log_value("loss", config.get("loss", 0.5))
    return {"loss": config.get("loss", 0.5)}
"""

_CONFIG_SCAFFOLD = """base = {"loss": 0.5}

n_trials = 3
objective = lambda results: results["loss"]
direction = "minimize"
"""
# ── init ─────────────────────────────────────────────────────────────────────


@app.command("init")
def init(
    project_dir: Annotated[
        str, typer.Argument(help="Directory to initialize (default: current)")
    ] = ".",
    starter: Annotated[
        str, typer.Option("--starter", "-s", help="Container starter to use")
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

    if starter not in list_starters():
        print(
            f"Error: Unknown starter: {starter}. "
            f"Available: {', '.join(list_starters())}"
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

    _copy_starter(project_path, starter, ".def", "container.def")
    _copy_starter(project_path, starter, ".Dockerfile", "Dockerfile")

    src_dir = project_path / "src"
    if not src_dir.exists():
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "__init__.py").write_text('"""Project source."""\n')
        print("Created: src/")
    else:
        print("Skipped: src/ (already exists)")

    trial_path = project_path / "trial.py"
    if trial_path.exists():
        print("Skipped: trial.py (already exists)")
    else:
        trial_path.write_text(_TRIAL_SCAFFOLD)
        print("Created: trial.py")

    sweep_path = project_path / "config.py"
    if sweep_path.exists():
        print("Skipped: config.py (already exists)")
    else:
        sweep_path.write_text(_CONFIG_SCAFFOLD)
        print("Created: config.py")

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
    print("  2. Run 'jernerics local trial.py config.py' to test the scaffold")
    print("  3. Run 'jernerics build --backend <name>' to build on remote")


def _get_default_jernerics_config(project_name: str) -> dict:
    return {
        "backends": {
            "hpc": {
                "type": "slurm",
                "host": "your-username@hpc.example.edu",
                "remote_dir": f"~/experiments/{project_name}",
                "cache_dir": "/scratch/$USER/jernerics",
                "slurm": {
                    "partition": "priority",
                    "time": "1:00:00",
                    "mem": "16G",
                    "cpus": 4,
                },
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
