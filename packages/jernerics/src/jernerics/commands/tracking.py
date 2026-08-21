import json
import os
import re
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from jernerics_schema import (
    ExecutionRecord,
    TrialRecord,
    ValueRecord,
)
from rich.console import Console

from jernerics.commands.common import _get_backend
from jernerics.config import (
    ExitCode,
    find_pyproject_dir,
    get_project_name,
    load_tracking_server,
)
from jernerics.observability.render import (
    render_diff,
    render_query,
    render_runs,
    render_summary,
    render_trace,
)
from jernerics.paths import cache_dir
from jernerics.tracking import ProjectHandle, TrackingClient, TrackingClientError
from jernerics.tracking.batch_sync import discover_jsonl_files, replay_tracking
from jernerics.tracking.infra import (
    TrackingServerSchemeError,
    resolve_tracking_ship,
)
from jernerics.tracking.jsonl_io import TrackingReader

# ── replay ───────────────────────────────────────────────────────────────────


def _count_local_events(tracking_dir: Path, study: str | None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in discover_jsonl_files(tracking_dir, study):
        study_name = path.parent.parent.name
        with TrackingReader(path) as reader:
            counts[study_name] = counts.get(study_name, 0) + sum(1 for _ in reader)
    return counts


def _run_dry_run(
    tracking_dir: Path,
    study: str | None,
    json_output: bool,
) -> None:
    local_counts = _count_local_events(tracking_dir, study)
    report = [
        {"study": name, "local": count} for name, count in sorted(local_counts.items())
    ]

    if json_output:
        print(json.dumps(report, indent=2))
        return

    if not report:
        print("No local events found. Nothing to replay.")
        return

    total_local = sum(r["local"] for r in report)
    for r in report:
        print(f"  {r['study']}: {r['local']} local events")
    print(f"\nTotal: {total_local} local events (dry run — nothing sent)")


def replay(
    study: Annotated[
        str | None,
        typer.Option("--study", "-s", help="Scope replay to a single study"),
    ] = None,
    backend_name: Annotated[
        str | None,
        typer.Option(
            "--backend",
            "-b",
            help="Pull the backend's remote tracking cache, then ship to server",
        ),
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
    """Replay tracked events to the server.

    Without ``--backend`` replays the local tracking cache. With ``--backend``
    pulls that backend's remote tracking cache, then ships it to the server.
    """
    if backend_name is not None:
        rejected = [
            flag
            for flag, value in (
                ("--tracking-dir", tracking_dir),
                ("--server", server),
                ("--dry-run", dry_run),
                ("--json", json_output),
            )
            if value
        ]
        if rejected:
            print(f"Error: {' '.join(rejected)} cannot be combined with --backend.")
            raise SystemExit(ExitCode.GENERAL_ERROR)
        backend, _, project_dir = _get_backend(backend_name)
        project_name = get_project_name(project_dir)
        backend.sync(project_name, study=study)
        return

    try:
        ship = resolve_tracking_ship(server or load_tracking_server() or "")
    except TrackingServerSchemeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None
    if ship is None:
        print(
            "Error: No tracking server configured. Set JERNERICS_TRACKING_SERVER "
            "or [tool.jernerics] tracking_server in pyproject.toml."
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)
    base_url, api_key = ship

    resolved_dir = tracking_dir or (cache_dir() / "tracking")

    if dry_run:
        _run_dry_run(resolved_dir, study, json_output)
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


class TrackingRefError(Exception):
    """A trial reference could not be resolved to exactly one trial."""


_HEX_TRIAL_ID = re.compile(
    r"^([0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

_MONITORING_PRECEDENCE = ("active", "stale", "quiet", "unknown", "ended")


def _fail(message: str) -> NoReturn:
    print(f"Error: {message}")
    raise SystemExit(ExitCode.GENERAL_ERROR)


def _open_tracking_client() -> tuple[TrackingClient, str]:
    """Client plus project name via the pyproject/env config chain."""
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
    previous = os.environ.get("JERNERICS_TRACKING_SERVER")
    os.environ["JERNERICS_TRACKING_SERVER"] = server_url
    try:
        client = TrackingClient.from_env()
    except TrackingClientError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None
    finally:
        if previous is None:
            del os.environ["JERNERICS_TRACKING_SERVER"]
        else:
            os.environ["JERNERICS_TRACKING_SERVER"] = previous
    return client, project_name


def _resolve_trial(handle: ProjectHandle, ref: str) -> TrialRecord:
    """Resolve ``<sweep-name>:<trial-number>`` or a raw 32-hex trial id."""
    if _HEX_TRIAL_ID.match(ref):
        records = handle.trials(handle.for_trials(uuid.UUID(ref)))
        if len(records) == 1:
            return records[0]
        raise TrackingRefError(f"no trial with id {ref} in this project")
    name, sep, number_text = ref.rpartition(":")
    if not sep or not name:
        raise TrackingRefError(
            f"trial ref {ref!r} must be '<sweep-name>:<trial-number>' "
            "or a 32-hex trial id"
        )
    try:
        number = int(number_text)
    except ValueError:
        raise TrackingRefError(
            f"trial ref {ref!r} has a non-integer trial number {number_text!r}"
        ) from None
    if number < 0:
        raise TrackingRefError(
            f"trial ref {ref!r} has a negative trial number {number_text!r}"
        )
    sweeps = [sweep for sweep in handle.sweeps() if sweep.name == name]
    if not sweeps:
        raise TrackingRefError(f"no sweep named {name!r} in this project")
    records = [
        trial
        for trial in handle.trials(handle.for_sweeps(sweeps[0].sweep_id))
        if trial.number == number
    ]
    if len(records) == 1:
        return records[0]
    raise TrackingRefError(f"no trial {number} in sweep {name!r}")


def _monitoring_fold(executions: list[ExecutionRecord]) -> str:
    """One monitoring label for a trial from its executions' labels."""
    labels = {execution.monitoring for execution in executions}
    for label in _MONITORING_PRECEDENCE:
        if label in labels:
            return label
    return "unknown"


def _instant_ns(value: Any) -> int | None:
    if value is None:
        return None
    return int(value.timestamp() * 1_000_000_000)


def _updated_ns(executions: list[ExecutionRecord]) -> int | None:
    stamps = [
        stamp
        for execution in executions
        for stamp in (
            execution.last_observation_ns,
            execution.last_heartbeat_ns,
            _instant_ns(execution.ended_at),
            _instant_ns(execution.started_at),
        )
        if stamp is not None
    ]
    return max(stamps, default=None)


def _run_rows(handle: ProjectHandle) -> list[dict[str, Any]]:
    """One view row per trial: the trial dump plus derived listing columns."""
    sweep_names = {sweep.sweep_id: sweep.name for sweep in handle.sweeps()}
    trials = handle.trials()
    refs = {
        trial.trial_id: (
            f"{sweep_names.get(trial.sweep_id, str(trial.sweep_id))}:{trial.number}"
        )
        for trial in trials
    }
    executions_by_trial: dict[uuid.UUID, list[ExecutionRecord]] = defaultdict(list)
    for execution in handle.executions():
        executions_by_trial[execution.trial_id].append(execution)
    param_counts = Counter(param.trial_id for param in handle.params())
    value_counts = Counter(value.trial_id for value in handle.values())
    rows: list[dict[str, Any]] = []
    for trial in sorted(
        trials, key=lambda t: (sweep_names.get(t.sweep_id, ""), t.number)
    ):
        trial_executions = executions_by_trial.get(trial.trial_id, [])
        rows.append(
            {
                "trial": trial.model_dump(mode="json"),
                "sweep": sweep_names.get(trial.sweep_id, str(trial.sweep_id)),
                "number": trial.number,
                "monitoring": _monitoring_fold(trial_executions),
                "params": param_counts.get(trial.trial_id, 0),
                "values": value_counts.get(trial.trial_id, 0),
                "updated_ns": _updated_ns(trial_executions),
                "root": refs.get(
                    trial.retry_root_trial_id, str(trial.retry_root_trial_id)[:8]
                ),
            }
        )
    return rows


def _trial_ref(handle: ProjectHandle, trial: TrialRecord) -> str:
    sweeps = handle.sweeps(handle.for_sweeps(trial.sweep_id))
    name = sweeps[0].name if sweeps else str(trial.sweep_id)[:8]
    return f"{name}:{trial.number}"


def _summary_payload(handle: ProjectHandle, trial: TrialRecord) -> dict[str, Any]:
    trial_values = handle.for_trials(trial.trial_id)
    family = handle.lineage(handle.for_retry_roots(trial.retry_root_trial_id))
    family.sort(key=lambda record: record.retry_index)
    params = sorted(
        handle.params(trial_values), key=lambda record: (record.kind, record.key)
    )
    catalog = sorted(handle.value_catalog(trial_values), key=lambda r: r.key)
    artifacts = sorted(handle.artifacts(trial_values), key=lambda r: r.key)
    executions = sorted(handle.executions(trial_values), key=lambda r: r.started_at)
    return {
        "trial": trial.model_dump(mode="json"),
        "sweep": _trial_ref(handle, trial).rpartition(":")[0],
        "label": _trial_ref(handle, trial),
        "lineage": [record.model_dump(mode="json") for record in family],
        "params": [record.model_dump(mode="json") for record in params],
        "values": [record.model_dump(mode="json") for record in catalog],
        "artifacts": [record.model_dump(mode="json") for record in artifacts],
        "executions": [record.model_dump(mode="json") for record in executions],
    }


def _latest_value(record: ValueRecord | None) -> Any:
    if record is None:
        return None
    if record.value is not None:
        return record.value
    return json.dumps(record.observation, sort_keys=True, separators=(",", ":"))


def _diff_payload(handle: ProjectHandle, a: TrialRecord, b: TrialRecord) -> dict:
    a_params = {
        param.key: param for param in handle.params(handle.for_trials(a.trial_id))
    }
    b_params = {
        param.key: param for param in handle.params(handle.for_trials(b.trial_id))
    }
    a_latest = handle.latest_values(handle.for_trials(a.trial_id))
    b_latest = handle.latest_values(handle.for_trials(b.trial_id))
    return {
        "a": a.model_dump(mode="json"),
        "b": b.model_dump(mode="json"),
        "a_label": _trial_ref(handle, a),
        "b_label": _trial_ref(handle, b),
        "params": [
            {
                "key": key,
                "a": a_params[key].value if key in a_params else None,
                "b": b_params[key].value if key in b_params else None,
            }
            for key in sorted(a_params.keys() | b_params.keys())
        ],
        "values": [
            {
                "key": key,
                "a": _latest_value(a_latest.get(key)),
                "b": _latest_value(b_latest.get(key)),
            }
            for key in sorted(a_latest.keys() | b_latest.keys())
        ],
        "objective": {"a": a.objective, "b": b.objective},
    }


def _trace_payload(handle: ProjectHandle, trial: TrialRecord, key: str) -> dict:
    records = handle.values(handle.for_trials(trial.trial_id), keys=(key,))
    records.sort(key=lambda r: (r.step, str(r.execution_id or "")))
    return {
        "trial_id": str(trial.trial_id),
        "label": _trial_ref(handle, trial),
        "key": key,
        "series": [
            {
                "step": record.step,
                "value": (
                    record.value
                    if record.value is not None
                    else json.dumps(
                        record.observation, sort_keys=True, separators=(",", ":")
                    )
                ),
            }
            for record in records
        ],
    }


def runs(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """List this project's trials with derived execution monitoring."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            rows = _run_rows(handle)
        except TrackingClientError as e:
            _fail(str(e))
    if json_output:
        payload = [{**row["trial"], "monitoring": row["monitoring"]} for row in rows]
        print(json.dumps(payload, indent=2))
        return
    render_runs(rows, Console())


def summary(
    trial_ref: Annotated[
        str,
        typer.Argument(
            help="Trial ref: '<sweep-name>:<trial-number>' or a 32-hex trial id"
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show one trial's lineage, params, values, artifacts, and executions."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            trial = _resolve_trial(handle, trial_ref)
            data = _summary_payload(handle, trial)
        except (TrackingClientError, TrackingRefError) as e:
            _fail(str(e))
    if json_output:
        print(json.dumps(data, indent=2))
        return
    render_summary(data, Console())


def diff(
    trial_ref_a: Annotated[
        str,
        typer.Argument(help="First trial ref ('<sweep>:<number>' or 32-hex id)"),
    ],
    trial_ref_b: Annotated[
        str,
        typer.Argument(
            help="Second trial ref ('<sweep-name>:<trial-number>' or hex id)"
        ),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Compare two trials: params union, latest values, and objective."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            trial_a = _resolve_trial(handle, trial_ref_a)
            trial_b = _resolve_trial(handle, trial_ref_b)
            data = _diff_payload(handle, trial_a, trial_b)
        except (TrackingClientError, TrackingRefError) as e:
            _fail(str(e))
    if json_output:
        print(json.dumps(data, indent=2))
        return
    render_diff(data, Console())


def trace(
    trial_ref: Annotated[
        str,
        typer.Argument(
            help="Trial ref: '<sweep-name>:<trial-number>' or a 32-hex trial id"
        ),
    ],
    key: Annotated[str, typer.Argument(help="Value key to trace")],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Show one value key's step series for a trial."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            trial = _resolve_trial(handle, trial_ref)
            data = _trace_payload(handle, trial, key)
        except (TrackingClientError, TrackingRefError) as e:
            _fail(str(e))
    if not data["series"]:
        _fail(f"no values for key {key!r} in trial {trial_ref!r}")
    if json_output:
        print(json.dumps(data, indent=2))
        return
    render_trace(data, Console())


def query(
    sql: Annotated[
        str, typer.Argument(help="One read-only SQL statement against the store")
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Run one raw read-only SQL statement (expert escape hatch).

    The routine commands (runs, summary, diff, trace) never use SQL — they
    read typed records over the domain endpoints. Use this only when the
    typed surface cannot answer your question.
    """
    client, _ = _open_tracking_client()
    with client:
        try:
            result = client.raw_query(sql)
        except TrackingClientError as e:
            _fail(str(e))
    if json_output:
        print(json.dumps({"columns": result["columns"], "rows": result["rows"]}))
        return
    render_query(result["columns"], result["rows"], Console())


def register(app: typer.Typer) -> None:
    group = typer.Typer(help="Replay tracking data and inspect tracked trials")
    group.command("replay")(replay)
    group.command("runs")(runs)
    group.command("summary")(summary)
    group.command("diff")(diff)
    group.command("trace")(trace)
    group.command("query")(query)
    app.add_typer(group, name="tracking")
