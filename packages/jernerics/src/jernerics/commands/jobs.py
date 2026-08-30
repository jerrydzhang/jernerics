import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from jernerics.backend.job_meta import load_job_studies
from jernerics.backend.slurm.sacct import (
    JobResourceSnapshot,
    build_job_resource_event,
    fetch_job_resources,
)
from jernerics.commands.common import _get_backend
from jernerics.config import ExitCode, load_tracking_server
from jernerics.paths import cache_dir
from jernerics.tracking.batch_sync import ship_events_file
from jernerics.tracking.infra import (
    TrackingServerSchemeError,
    resolve_tracking_ship,
)

# ── jobs ─────────────────────────────────────────────────────────────────────


def list_jobs(
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
    """List jobs known to a backend."""
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
    """Cancel one job by ID, or all jobs with --all."""
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
    """Stream a job's stdout (or stderr) from the backend."""
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
    """Block until a job finishes, succeeding only if it completed."""
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


# ── resources ────────────────────────────────────────────────────────────────


def _print_resources(snapshot: JobResourceSnapshot) -> None:
    fields = [
        ("job_id", snapshot.job_id),
        ("state", snapshot.state),
        ("exit_code", snapshot.exit_code),
        ("wall_time_s", snapshot.wall_time_s),
        ("cpu_time_s", snapshot.cpu_time_s),
        ("cpu_pct", snapshot.cpu_pct),
        ("max_rss_mb", snapshot.max_rss_mb),
        ("ave_rss_mb", snapshot.ave_rss_mb),
        ("alloc_cpus", snapshot.alloc_cpus),
        ("req_mem", snapshot.req_mem),
        ("alloc_tres", snapshot.alloc_tres),
        ("node_list", snapshot.node_list),
    ]
    width = max(len(name) for name, _ in fields)
    for name, value in fields:
        print(f"{name:<{width}}  {value if value is not None else '—'}")


def _ship_resources(snapshot: JobResourceSnapshot) -> None:
    server_addr = load_tracking_server()
    try:
        resolved = resolve_tracking_ship(server_addr) if server_addr else None
    except TrackingServerSchemeError as error:
        print(f"Cannot ship resource record: {error}")
        return
    if resolved is None:
        print("No tracking server configured; resource record not shipped.")
        return
    base_url, api_key = resolved
    study_name = load_job_studies(cache_dir()).get(snapshot.job_id)
    event = build_job_resource_event(snapshot, study_name=study_name)
    submission_dir = (
        cache_dir() / "tracking" / study_name / "submission" if study_name else None
    )
    if submission_dir is not None:
        submission_dir.mkdir(parents=True, exist_ok=True)
        path = submission_dir / f"resources-{uuid.uuid4().hex[:8]}.jsonl"
        path.write_text(event.model_dump_json() + "\n")
    else:
        scratch = TemporaryDirectory()
        path = Path(scratch.name) / "resources.jsonl"
        path.write_text(event.model_dump_json() + "\n")
    try:
        if ship_events_file(path, base_url, api_key):
            print(f"Resource record for job {snapshot.job_id} shipped.")
    finally:
        if submission_dir is None:
            scratch.cleanup()


def resources(
    job_id: Annotated[str, typer.Argument(help="Scheduler job ID")],
    ship: Annotated[
        bool,
        typer.Option("--ship", help="Also append the record to the tracking server"),
    ] = False,
) -> None:
    """Show sacct resource usage for a past job within retention."""
    result = fetch_job_resources(job_id)
    if result.snapshot is None:
        print(f"No accounting data for job {job_id}: {result.error}")
        return
    _print_resources(result.snapshot)
    if ship:
        _ship_resources(result.snapshot)


def register(app: typer.Typer) -> None:
    group = typer.Typer(help="Inspect and manage backend jobs")
    group.command("list")(list_jobs)
    group.command("cancel")(cancel)
    group.command("logs")(logs)
    group.command("wait")(wait)
    group.command("resources")(resources)
    app.add_typer(group, name="job")
