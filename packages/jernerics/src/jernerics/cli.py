import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

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
from .config import (
    ConfigNotFound,
    ExitCode,
    find_pyproject_dir,
    get_project_name,
    load_backend_config,
    load_config,
    load_tracking_http_server,
    load_tracking_server,
)
from .container.templates import get_starter, list_starters
from .paths import cache_dir

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")


SAFE_RELPATH = re.compile(r"^[a-zA-Z0-9_./\-]+$")


def _resolve_tracking_server_url(server: str | None) -> str:
    """Resolve tracking HTTP server URL from --server arg, env var, or config.

    Resolution order:
    1. --server argument (if provided)
    2. JERNERICS_TRACKING_HTTP_SERVER environment variable
    3. [tool.jernerics].tracking_http_server in pyproject.toml

    Raises SystemExit with ExitCode.CONFIG_ERROR if no URL is found.
    """
    base_url = server
    if base_url is None:
        base_url = os.environ.get("JERNERICS_TRACKING_HTTP_SERVER")
    if base_url is None:
        project_dir = find_pyproject_dir()
        if project_dir is not None:
            base_url = load_tracking_http_server(project_dir)

    if base_url is None:
        print(
            "Error: No tracking HTTP server URL configured. "
            "Set --server, JERNERICS_TRACKING_HTTP_SERVER, "
            "or tracking_http_server in pyproject.toml"
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    return base_url


def _resolve_project_name(project: str | None) -> str:
    """Resolve project name from --project arg or default to current pyproject.toml.

    If --project is provided, returns it as-is.
    Otherwise, looks up project name from the nearest pyproject.toml.

    Raises SystemExit with ExitCode.CONFIG_ERROR if no pyproject.toml is found.
    """
    if project is None:
        project_dir = find_pyproject_dir()
        if project_dir is None:
            print(
                "Error: No pyproject.toml found. "
                "Either provide --project or run from a Jernerics project directory."
            )
            raise SystemExit(ExitCode.CONFIG_ERROR)
        project = get_project_name(project_dir)

    return project


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


def _get_backend(backend_name: str) -> tuple[Backend, str, Path]:
    """Load a backend by name. Returns (backend, project_name, project_dir)."""
    from .backend.factory import make_backend

    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        config = load_backend_config(backend_name, project_dir)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_name = get_project_name(project_dir)
    tracking_server = load_tracking_server(project_dir)

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

    project_cache = cache_dir()
    optuna_dir = project_cache / "optuna"
    optuna_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"local_{config_path.stem}_{timestamp}"
    storage_path = str(optuna_dir / (study_name + ".journal"))

    spec = SweepSubmission(
        dag_path=dag_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_path,
        n_trials=sweep.n_trials,
        project_name=project_name,
        server_addr=tracking_server,
        grid=sweep.grid,
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

    dag_relpath = _validate_relpath(str(dag_path.relative_to(project_dir)), "DAG file")
    config_relpath = _validate_relpath(
        str(config_path.relative_to(project_dir)), "Config file"
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"{project_name}_{config_path.stem}_{timestamp}"
    storage_url = backend.storage_path(study_name)

    spec = SweepSubmission(
        dag_path=dag_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_url,
        n_trials=sweep.n_trials,
        dag_relpath=dag_relpath,
        config_relpath=config_relpath,
        project_name=project_name,
        server_addr=backend.tracking_server,
        grid=sweep.grid,
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


# ── sweeps ───────────────────────────────────────────────────────────────────


@app.command("sweeps")
def sweeps(
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Filter sweeps by project name"),
    ] = None,
):
    """List sweeps from the tracking HTTP server."""
    from .tracking.http_api import list_sweeps

    base_url = _resolve_tracking_server_url(server)

    try:
        sweeps_data = list_sweeps(base_url, project=project)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(sweeps_data, indent=2))
        return

    if not sweeps_data:
        print("No sweeps found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("PROJECT")
    table.add_column("STUDY_NAME")
    table.add_column("TRIALS")
    table.add_column("COMPLETED")
    table.add_column("LAST_EVENT")

    for sweep in sweeps_data:
        table.add_row(
            sweep.get("project", ""),
            sweep.get("study_name", ""),
            str(sweep.get("trial_count", 0)),
            str(sweep.get("completed_count", 0)),
            sweep.get("last_event_timestamp_ns", ""),
        )

    Console().print(table)


# ── trials ───────────────────────────────────────────────────────────────────


@app.command("trials")
def trials(
    sweep: Annotated[str, typer.Option(help="Sweep/study name")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    params: Annotated[
        bool,
        typer.Option("--params", help="Include param columns in human table"),
    ] = False,
    columns: Annotated[
        str | None,
        typer.Option("--columns", help="Comma-separated column projection"),
    ] = None,
    metrics: Annotated[
        str | None,
        typer.Option("--metrics", help="Comma-separated metric keys to filter"),
    ] = None,
    limit: Annotated[
        int,
        typer.Option("--limit", help="Maximum trials to show (default 100)"),
    ] = 100,
    status: Annotated[
        str | None,
        typer.Option("--status", help="Filter by status: complete or incomplete"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """List trials from the tracking HTTP server."""
    from .tracking.http_api import list_trials

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    try:
        trials_data = list_trials(
            base_url, project, sweep, limit=limit, metric_keys=metrics, status=status
        )
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(trials_data, indent=2))
        return

    if not trials_data:
        print("No trials found.")
        return

    if columns:
        selected_columns = [c.strip() for c in columns.split(",")]
    else:
        selected_columns = ["trial_id", "status"]
        if params:
            for trial in trials_data:
                for param_key in trial.get("params", {}):
                    if param_key not in selected_columns:
                        selected_columns.append(param_key)
        for trial in trials_data:
            for metric_key in trial.get("final_metrics", {}):
                if metric_key not in selected_columns:
                    selected_columns.append(metric_key)
        if "artifact_count" not in selected_columns:
            selected_columns.append("artifact_count")

    table = Table(show_header=True, header_style="bold")
    for col in selected_columns:
        table.add_column(col.upper())

    for trial in trials_data:
        row_values = []
        for col in selected_columns:
            if col == "trial_id":
                row_values.append(str(trial.get("trial_id", "")))
            elif col == "status":
                row_values.append(trial.get("status", ""))
            elif col == "artifact_count":
                row_values.append(str(len(trial.get("artifact_keys", []))))
            elif col in trial.get("params", {}):
                row_values.append(str(trial["params"][col]))
            elif col in trial.get("final_metrics", {}):
                row_values.append(str(trial["final_metrics"][col]))
            else:
                row_values.append("")
        table.add_row(*row_values)

    Console().print(table)


# ── compare-sweeps ───────────────────────────────────────────────────────────


@app.command("compare-sweeps")
def compare_sweeps(
    left: Annotated[str, typer.Argument(help="Left sweep/study name")],
    right: Annotated[str, typer.Argument(help="Right sweep/study name")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    metrics: Annotated[
        str | None,
        typer.Option("--metrics", help="Comma-separated metric keys to filter"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """Compare two sweeps from the tracking HTTP server."""
    from .tracking.http_api import compare_sweeps as compare_sweeps_api

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    metrics_list = None
    if metrics is not None:
        metrics_list = [m.strip() for m in metrics.split(",") if m.strip()]

    try:
        comparison = compare_sweeps_api(
            base_url, project, left, right, metrics=metrics_list
        )
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(comparison, indent=2))
        return

    console = Console()

    console.print()
    console.print(f"[bold]Sweep Comparison:[/bold] {left} vs {right}")
    console.print()

    counts_table = Table(show_header=True, header_style="bold")
    counts_table.add_column("")
    counts_table.add_column(left, style="cyan")
    counts_table.add_column(right, style="magenta")
    counts_table.add_row(
        "Trials",
        str(comparison["left_trial_count"]),
        str(comparison["right_trial_count"]),
    )
    counts_table.add_row(
        "Completed",
        str(comparison["left_completed_count"]),
        str(comparison["right_completed_count"]),
    )
    console.print(counts_table)
    console.print()

    param_keys = comparison["param_keys"]
    if param_keys["shared"] or param_keys["left_only"] or param_keys["right_only"]:
        param_table = Table(show_header=True, header_style="bold")
        param_table.add_column("Param Keys")
        param_table.add_column(left, style="cyan")
        param_table.add_column(right, style="magenta")
        param_table.add_column("Shared")

        all_keys = sorted(
            set(
                param_keys["shared"]
                + param_keys["left_only"]
                + param_keys["right_only"]
            )
        )
        for key in all_keys:
            left_mark = (
                "✓"
                if key in param_keys["shared"] or key in param_keys["left_only"]
                else ""
            )
            right_mark = (
                "✓"
                if key in param_keys["shared"] or key in param_keys["right_only"]
                else ""
            )
            shared_mark = "✓" if key in param_keys["shared"] else ""
            param_table.add_row(key, left_mark, right_mark, shared_mark)

        console.print("[bold]Parameter Keys:[/bold]")
        console.print(param_table)
        console.print()

    metric_keys = comparison["final_metric_keys"]
    if metric_keys["shared"] or metric_keys["left_only"] or metric_keys["right_only"]:
        metric_table = Table(show_header=True, header_style="bold")
        metric_table.add_column("Metric Keys")
        metric_table.add_column(left, style="cyan")
        metric_table.add_column(right, style="magenta")
        metric_table.add_column("Shared")

        all_keys = sorted(
            set(
                metric_keys["shared"]
                + metric_keys["left_only"]
                + metric_keys["right_only"]
            )
        )
        for key in all_keys:
            left_mark = (
                "✓"
                if key in metric_keys["shared"] or key in metric_keys["left_only"]
                else ""
            )
            right_mark = (
                "✓"
                if key in metric_keys["shared"] or key in metric_keys["right_only"]
                else ""
            )
            shared_mark = "✓" if key in metric_keys["shared"] else ""
            metric_table.add_row(key, left_mark, right_mark, shared_mark)

        console.print("[bold]Final Metric Keys:[/bold]")
        console.print(metric_table)
        console.print()

    artifact_keys = comparison["artifact_keys"]
    if (
        artifact_keys["shared"]
        or artifact_keys["left_only"]
        or artifact_keys["right_only"]
    ):
        artifact_table = Table(show_header=True, header_style="bold")
        artifact_table.add_column("Artifact Keys")
        artifact_table.add_column(left, style="cyan")
        artifact_table.add_column(right, style="magenta")
        artifact_table.add_column("Shared")

        all_keys = sorted(
            set(
                artifact_keys["shared"]
                + artifact_keys["left_only"]
                + artifact_keys["right_only"]
            )
        )
        for key in all_keys:
            left_mark = (
                "✓"
                if key in artifact_keys["shared"] or key in artifact_keys["left_only"]
                else ""
            )
            right_mark = (
                "✓"
                if key in artifact_keys["shared"] or key in artifact_keys["right_only"]
                else ""
            )
            shared_mark = "✓" if key in artifact_keys["shared"] else ""
            artifact_table.add_row(key, left_mark, right_mark, shared_mark)

        console.print("[bold]Artifact Keys:[/bold]")
        console.print(artifact_table)
        console.print()

    metric_stats = comparison["final_metric_stats"]
    if metric_stats:
        stats_table = Table(show_header=True, header_style="bold")
        stats_table.add_column("Metric")
        stats_table.add_column(f"{left} (min/median/max)", style="cyan")
        stats_table.add_column(f"{right} (min/median/max)", style="magenta")

        for metric_name, stats in sorted(metric_stats.items()):
            left_stats = stats["left"]
            right_stats = stats["right"]
            left_str = (
                f"{left_stats['min']:.4f} / {left_stats['median']:.4f} "
                f"/ {left_stats['max']:.4f}"
            )
            right_str = (
                f"{right_stats['min']:.4f} / {right_stats['median']:.4f} "
                f"/ {right_stats['max']:.4f}"
            )
            stats_table.add_row(metric_name, left_str, right_str)

        console.print("[bold]Shared Final Metrics (min/median/max):[/bold]")
        console.print(stats_table)
        console.print()


# ── sweep-summary ────────────────────────────────────────────────────────────────


@app.command("sweep-summary")
def sweep_summary(
    sweep: Annotated[str, typer.Option(help="Sweep/study name")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """Get a factual summary of a sweep from the tracking HTTP server."""
    from .tracking.http_api import get_sweep_summary

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    try:
        summary_data = get_sweep_summary(base_url, project, sweep)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(summary_data, indent=2))
        return

    console = Console()

    console.print()
    console.print(f"[bold]Sweep Summary:[/bold] {summary_data['study_name']}")
    console.print()

    console.print(f"  Project: {summary_data['project']}")
    console.print(
        f"  Trials: {summary_data['trial_count']} "
        f"({summary_data['completed_count']} completed)"
    )
    console.print()

    if summary_data["param_keys"]:
        console.print("[bold]Parameter Keys:[/bold]")
        for key in summary_data["param_keys"]:
            console.print(f"  - {key}")
        console.print()

    if summary_data["final_metric_keys"]:
        console.print("[bold]Final Metric Keys:[/bold]")
        for key in summary_data["final_metric_keys"]:
            console.print(f"  - {key}")
        console.print()

    if summary_data["artifact_keys"]:
        console.print("[bold]Artifact Keys:[/bold]")
        for key in summary_data["artifact_keys"]:
            console.print(f"  - {key}")
        console.print()


# ── metric-history ───────────────────────────────────────────────────────────────


@app.command("metric-history")
def metric_history(
    sweep: Annotated[str, typer.Option(help="Sweep/study name")],
    metric: Annotated[str, typer.Option(help="Metric key")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """Get metric history from the tracking HTTP server."""
    from .tracking.http_api import get_metric_history

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    try:
        history_data = get_metric_history(base_url, project, sweep, metric)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(history_data, indent=2))
        return

    if not history_data:
        print("No metric history found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("TRIAL_ID")
    table.add_column("STEP")
    table.add_column("VALUE")
    table.add_column("TIMESTAMP_NS")

    for entry in history_data:
        table.add_row(
            str(entry.get("trial_id", "")),
            str(entry.get("step", "")),
            str(entry.get("value", "")),
            str(entry.get("timestamp_ns", "")),
        )

    Console().print(table)


# ── tracking-health ─────────────────────────────────────────────────────────────


@app.command("tracking-health")
def tracking_health(
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """Check health of the tracking HTTP server."""
    from .tracking.http_api import get_health

    base_url = _resolve_tracking_server_url(server)

    try:
        health_data = get_health(base_url)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(health_data, indent=2))
        return

    if health_data.get("ok"):
        print("Tracking server is OK")
    else:
        print("Tracking server is not OK")
        print(json.dumps(health_data, indent=2))


# ── artifacts ───────────────────────────────────────────────────────────────────


@app.command("artifacts")
def artifacts(
    sweep: Annotated[str, typer.Option(help="Sweep/study name")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    trial_id: Annotated[
        int | None,
        typer.Option("--trial-id", help="Filter artifacts by trial ID"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """List artifacts from the tracking HTTP server."""
    from .tracking.http_api import list_artifacts

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    try:
        artifacts_data = list_artifacts(base_url, project, sweep, trial_id=trial_id)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(artifacts_data, indent=2))
        return

    if not artifacts_data:
        print("No artifacts found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("TRIAL_ID")
    table.add_column("KEY")
    table.add_column("FILENAME")
    table.add_column("TIMESTAMP_NS")

    for artifact in artifacts_data:
        table.add_row(
            str(artifact.get("trial_id", "")),
            artifact.get("key", ""),
            artifact.get("filename", ""),
            str(artifact.get("timestamp_ns", "")),
        )

    Console().print(table)


# ── results ────────────────────────────────────────────────────────────────────


@app.command("results")
def results(
    sweep: Annotated[str, typer.Option(help="Sweep/study name")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    trial_id: Annotated[
        int | None,
        typer.Option("--trial-id", help="Filter results by trial ID"),
    ] = None,
    key: Annotated[
        str | None,
        typer.Option("--key", help="Filter results by key"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """List results from the tracking HTTP server."""
    from .tracking.http_api import list_results

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    try:
        results_data = list_results(
            base_url, project, sweep, trial_id=trial_id, key=key
        )
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(results_data, indent=2))
        return

    if not results_data:
        print("No results found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("TRIAL_ID")
    table.add_column("KEY")
    table.add_column("VALUE")
    table.add_column("TIMESTAMP_NS")

    for result in results_data:
        table.add_row(
            str(result.get("trial_id", "")),
            result.get("key", ""),
            result.get("value", ""),
            str(result.get("timestamp_ns", "")),
        )

    Console().print(table)


# ── params ────────────────────────────────────────────────────────────────────


@app.command("params")
def params(
    sweep: Annotated[str, typer.Option(help="Sweep/study name")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    trial_id: Annotated[
        int | None,
        typer.Option("--trial-id", help="Filter params by trial ID"),
    ] = None,
    key: Annotated[
        str | None,
        typer.Option("--key", help="Filter params by key"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """List params from the tracking HTTP server."""
    from .tracking.http_api import list_params

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    try:
        params_data = list_params(base_url, project, sweep, trial_id=trial_id, key=key)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(params_data, indent=2))
        return

    if not params_data:
        print("No params found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("TRIAL_ID")
    table.add_column("KEY")
    table.add_column("VALUE")
    table.add_column("TIMESTAMP_NS")

    for param in params_data:
        table.add_row(
            str(param.get("trial_id", "")),
            param.get("key", ""),
            str(param.get("value", "")),
            str(param.get("timestamp_ns", "")),
        )

    Console().print(table)


# ── metric-keys ─────────────────────────────────────────────────────────────────


@app.command("metric-keys")
def metric_keys(
    sweep: Annotated[str, typer.Option(help="Sweep/study name")],
    project: Annotated[
        str | None,
        typer.Option("--project", help="Project name (default: from pyproject.toml)"),
    ] = None,
    server: Annotated[
        str | None,
        typer.Option("--server", help="Tracking HTTP server URL"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    """List final metric keys from the tracking HTTP server."""
    from .tracking.http_api import list_metric_keys

    project = _resolve_project_name(project)
    base_url = _resolve_tracking_server_url(server)

    try:
        metric_keys_data = list_metric_keys(base_url, project, sweep)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if json_output:
        print(json.dumps(metric_keys_data, indent=2))
        return

    for key in metric_keys_data.get("final_metric_keys", []):
        print(key)


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
