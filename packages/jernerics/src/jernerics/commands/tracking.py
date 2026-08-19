import json
import os
from pathlib import Path
from typing import Annotated

import httpx
import typer
from rich.console import Console

from jernerics.commands.common import _get_backend
from jernerics.config import (
    ExitCode,
    find_pyproject_dir,
    get_project_name,
    load_tracking_server,
)
from jernerics.observability import (
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
from jernerics.paths import cache_dir
from jernerics.tracking.batch_sync import discover_jsonl_files, replay_tracking
from jernerics.tracking.jsonl_io import TrackingReader

# ── sync ─────────────────────────────────────────────────────────────────────


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


def register(app: typer.Typer) -> None:
    app.command("sync")(sync)
    app.command("replay")(replay)
    app.command("runs")(runs)
    app.command("summary")(summary)
    app.command("diff")(diff)
    app.command("trace")(trace)
