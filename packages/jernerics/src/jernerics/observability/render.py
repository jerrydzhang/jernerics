"""Rich renderers for the tracking observability CLI commands.

Every renderer takes the same plain payload the command's ``--json``
output emits (plus the derived listing columns) and prints colourised
tables; JSON formatting itself stays in the CLI.
"""

import time
from typing import Any

from rich.console import Console
from rich.table import Table


def _format_number(v: Any) -> str:
    """Human-readable number. Large magnitudes collapse to M/B; small
    floats keep significant figures. ``None`` renders as ``-``."""
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        av = abs(v)
        if av >= 1_000_000_000:
            return f"{v / 1e9:.1f}B"
        if av >= 1_000_000:
            return f"{v / 1e6:.1f}M"
        return str(v)
    if isinstance(v, float):
        av = abs(v)
        if av >= 1_000_000_000:
            return f"{v / 1e9:.1f}B"
        if av >= 1_000_000:
            return f"{v / 1e6:.1f}M"
        if av != 0 and av < 1e-4:
            return f"{v:.2e}"
        return f"{v:.4g}"
    return str(v)


def _format_value(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, str):
        return v
    return _format_number(v)


def _format_relative(ns: int | None) -> str:
    if ns is None:
        return "-"
    secs = (time.time_ns() - ns) / 1e9
    if secs < 0:
        return "now"
    if secs < 60:
        return f"{secs:.0f}s ago"
    if secs < 3600:
        return f"{secs / 60:.0f}m ago"
    if secs < 86400:
        return f"{secs / 3600:.0f}h ago"
    return f"{secs / 86400:.0f}d ago"


def _format_instant(iso: str | None) -> str:
    if iso is None:
        return "-"
    return iso.replace("T", " ").removesuffix("+00:00")


def _short_id(value: str) -> str:
    return value[:8]


def render_runs(rows: list[dict[str, Any]], console: Console) -> None:
    if not rows:
        console.print("No trials found.")
        return

    show_lineage = any(row["trial"]["retry_index"] for row in rows)
    table = Table(show_header=True, header_style="bold")
    table.add_column("SWEEP")
    table.add_column("TRIAL", justify="right")
    table.add_column("STATE")
    table.add_column("MONITORING")
    table.add_column("OBJECTIVE", justify="right")
    table.add_column("PARAMS", justify="right")
    table.add_column("VALUES", justify="right")
    table.add_column("UPDATED", justify="right")
    if show_lineage:
        table.add_column("RETRY", justify="right")
        table.add_column("ROOT")

    for row in rows:
        cells = [
            row["sweep"],
            str(row["number"]),
            row["trial"]["state"],
            row["monitoring"],
            _format_number(row["trial"]["objective"]),
            str(row["params"]),
            str(row["values"]),
            _format_relative(row["updated_ns"]),
        ]
        if show_lineage:
            cells += [str(row["trial"]["retry_index"]), row["root"]]
        table.add_row(*cells)

    console.print(table)


def render_summary(data: dict[str, Any], console: Console) -> None:
    trial = data["trial"]
    console.print(f"[bold]Trial {data['label']}[/bold] (sweep {data['sweep']})")
    console.print(
        f"  state: {trial['state']}    objective: {_format_number(trial['objective'])}"
    )

    lineage = data["lineage"]
    console.print(
        f"\nRetry lineage ({len(lineage)} generations, sweep {data['sweep']}):"
    )
    numbers = {record["trial_id"]: str(record["number"]) for record in lineage}
    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("GEN", justify="right")
    table.add_column("TRIAL", justify="right")
    table.add_column("PARENT", justify="right")
    table.add_column("ROOT", justify="right")
    for record in lineage:
        current = "*" if record["trial_id"] == trial["trial_id"] else ""
        parent = (
            numbers.get(record["retry_of_trial_id"])
            if record["retry_of_trial_id"]
            else "-"
        ) or _short_id(record["retry_of_trial_id"] or "")
        root = numbers.get(record["retry_root_trial_id"]) or _short_id(
            record["retry_root_trial_id"]
        )
        table.add_row(
            str(record["retry_index"]),
            str(record["number"]) + current,
            parent,
            root,
        )
    console.print(table)

    params = data["params"]
    console.print(f"\nParams ({len(params)}):")
    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("KEY")
    table.add_column("KIND")
    table.add_column("VALUE")
    for record in params:
        table.add_row(record["key"], record["kind"], _format_value(record["value"]))
    console.print(table)

    values = data["values"]
    console.print(f"\nValues ({len(values)}):")
    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("KEY")
    table.add_column("KIND")
    table.add_column("POINTS", justify="right")
    table.add_column("LATEST STEP", justify="right")
    for record in values:
        table.add_row(
            record["key"],
            record["kind"],
            str(record["n_points"]),
            str(record["latest_step"]),
        )
    console.print(table)

    artifacts = data["artifacts"]
    console.print(f"\nArtifacts ({len(artifacts)}):")
    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("KEY")
    table.add_column("FILENAME")
    table.add_column("SIZE", justify="right")
    table.add_column("RECEIVED")
    table.add_column("SOURCE")
    for record in artifacts:
        table.add_row(
            record["key"],
            record["filename"],
            _format_number(record["size_bytes"]),
            "yes" if record["received_ns"] is not None else "no",
            record["source"],
        )
    console.print(table)

    executions = data["executions"]
    console.print(f"\nExecutions ({len(executions)}):")
    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("HOST")
    table.add_column("STARTED")
    table.add_column("ENDED")
    table.add_column("OUTCOME")
    table.add_column("MONITORING")
    for record in executions:
        table.add_row(
            record["hostname"],
            _format_instant(record["started_at"]),
            _format_instant(record["ended_at"]),
            record["outcome"] or "-",
            record["monitoring"] or "-",
        )
    console.print(table)


def render_diff(data: dict[str, Any], console: Console) -> None:
    a, b = data["a"], data["b"]
    console.print(f"A: {data['a_label']} ({a['state']})")
    console.print(f"B: {data['b_label']} ({b['state']})")

    console.print(f"\nParams (union of {len(data['params'])}):")
    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("KEY")
    table.add_column("A", justify="right")
    table.add_column("B", justify="right")
    for entry in data["params"]:
        table.add_row(
            entry["key"],
            "(missing)" if entry["a"] is None else _format_value(entry["a"]),
            "(missing)" if entry["b"] is None else _format_value(entry["b"]),
        )
    console.print(table)

    console.print(f"\nValues (latest, union of {len(data['values'])}):")
    table = Table(show_header=True, header_style="bold", show_lines=False)
    table.add_column("KEY")
    table.add_column("A", justify="right")
    table.add_column("B", justify="right")
    for entry in data["values"]:
        table.add_row(
            entry["key"],
            "(missing)" if entry["a"] is None else _format_value(entry["a"]),
            "(missing)" if entry["b"] is None else _format_value(entry["b"]),
        )
    console.print(table)

    console.print(
        f"\nObjective: A {_format_number(data['objective']['a'])}    "
        f"B {_format_number(data['objective']['b'])}"
    )


def render_trace(data: dict[str, Any], console: Console) -> None:
    series = data["series"]
    console.print(f"Trace: {data['label']} / {data['key']} ({len(series)} points)")
    if not series:
        return
    step_width = len(str(max(point["step"] for point in series)))
    for point in series:
        console.print(
            f"  step {point['step']:>{step_width}}: {_format_value(point['value'])}"
        )


def render_query(columns: list[str], rows: list[list[Any]], console: Console) -> None:
    table = Table(show_header=True, header_style="bold")
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*(_format_value(value) for value in row))
    console.print(table)
