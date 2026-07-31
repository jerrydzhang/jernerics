"""Rich-based text rendering for runs/summary/diff.

JSON output is handled by the CLI directly (it dumps the raw analysis
dicts); these functions own the human-readable, colourised views.
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


def _format_params(params: dict[str, Any]) -> str:
    return "  ".join(f"{k}={_format_number(v)}" for k, v in sorted(params.items()))


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


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


def _format_slope(slope: float | None) -> str:
    if slope is None:
        return "-"
    return f"{slope:+.4g}/step"


def _slope_header(label: str, rng: list[int] | None) -> str:
    if rng is None:
        return f"Slope ({label})"
    return f"Slope [{rng[0]}-{rng[1]}]"


def _step_count(run: dict[str, Any]) -> str:
    lo, hi = run.get("min_step"), run.get("max_step")
    if hi is None:
        return "-"
    if lo is None:
        return str(hi)
    return str(hi - lo + 1)


def _priority_column_key(runs: list[dict[str, Any]]) -> str | None:
    for run in runs:
        key = run.get("priority_key")
        if key is not None:
            return key
    return None


def render_runs(runs: list[dict[str, Any]], console: Console) -> None:
    if not runs:
        console.print("No runs found.")
        return

    priority_key = _priority_column_key(runs)
    table = Table(show_header=True, header_style="bold")
    table.add_column("RUN")
    table.add_column("STATUS")
    table.add_column("STEPS", justify="right")
    if priority_key is not None:
        table.add_column(priority_key.upper(), justify="right")
    table.add_column("DURATION", justify="right")
    table.add_column("CREATED", justify="right")
    table.add_column("PARAMS", overflow="fold")

    for run in runs:
        cells = [
            run["label"],
            run["status"],
            _step_count(run),
        ]
        if priority_key is not None:
            if run.get("priority_key") == priority_key:
                cells.append(_format_number(run.get("priority_value")))
            else:
                cells.append("-")
        cells += [
            _format_duration(run.get("duration_s")),
            _format_relative(run.get("created_ns")),
            _format_params(run["params"]),
        ]
        table.add_row(*cells)

    console.print(table)


def render_summary(summary: dict[str, Any], console: Console) -> None:
    label = summary["label"]
    duration = summary.get("duration_s")
    lo, hi = summary.get("min_step"), summary.get("max_step")
    step_range = f"{lo}-{hi}" if lo is not None and hi is not None else "-"

    console.print(f"[bold]Run {label}[/bold]")
    console.print(
        f"  status: {summary['status']}    steps: {step_range}    "
        f"duration: {_format_duration_seconds(duration)}"
    )

    params = summary["params"]
    console.print(f"\nParams ({len(params)}):")
    console.print(f"  {_format_params(params)}")

    metrics = summary["metrics"]
    if metrics:
        console.print("\nMetrics:")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Metric")
        table.add_column("First", justify="right")
        table.add_column("Last", justify="right")
        table.add_column("Change", justify="right")
        early_rng = next(
            (m["early_range"] for m in metrics.values() if m["early_range"]),
            None,
        )
        recent_rng = next(
            (m["recent_range"] for m in metrics.values() if m["recent_range"]),
            None,
        )
        table.add_column(_slope_header("early", early_rng), justify="right")
        table.add_column(_slope_header("recent", recent_rng), justify="right")
        for key, m in metrics.items():
            table.add_row(
                key,
                _format_number(m["first"]),
                _format_number(m["last"]),
                _format_number(m["change"]),
                _format_slope(m["early_slope"]),
                _format_slope(m["recent_slope"]),
            )
        console.print(table)

    artifacts = summary["artifacts"]
    if artifacts:
        console.print("\nArtifacts:")
        for key in artifacts:
            console.print(f"  {key}")


def _format_duration_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    return f"{seconds:.1f}s"


def render_diff(diff: dict[str, Any], console: Console) -> None:
    a, b = diff["run_a"], diff["run_b"]
    console.print(
        f"Run A: {a['label']}  ({a['status']}, "
        f"{_step_count_from_max(a['max_step'])} steps)"
    )
    console.print(
        f"Run B: {b['label']}  ({b['status']}, "
        f"{_step_count_from_max(b['max_step'])} steps)"
    )

    param_diff = diff["param_diff"]
    if param_diff:
        console.print("\nParams that differ:")
        table = Table(show_header=True, header_style="bold", show_lines=False)
        table.add_column("Param")
        table.add_column("Run A", justify="right")
        table.add_column("Run B", justify="right")
        for entry in param_diff:
            table.add_row(
                entry["key"],
                _format_number(entry["a"]),
                _format_number(entry["b"]),
            )
        console.print(table)
    else:
        console.print("\nNo differing params.")

    matched = diff["param_match"]
    console.print(f"\nParams that match ({diff['param_match_count']}):")
    if matched:
        console.print("  " + "  ".join(sorted(matched)))

    metric_diff = diff["metric_diff"]
    if metric_diff:
        console.print("")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Metric")
        table.add_column("A (Last)", justify="right")
        table.add_column("B (Last)", justify="right")
        table.add_column("Change", justify="right")
        for entry in metric_diff:
            table.add_row(
                entry["key"],
                _format_number(entry["a"]),
                _format_number(entry["b"]),
                _format_number(entry["change"]),
            )
        console.print(table)


def _step_count_from_max(max_step: int | None) -> str:
    if max_step is None:
        return "?"
    return str(max_step + 1)


def render_trace(
    label: str,
    metric: str,
    series: list[dict[str, Any]],
    console: Console,
) -> None:
    """Human-readable trace: one line per step/value pair."""
    if not series:
        console.print(f"Trace: {label} / {metric} — no data")
        return

    console.print(f"Trace: {label} / {metric} ({len(series)} points)")
    max_step = max(
        (p["step"] for p in series if p["step"] is not None), default=None
    )
    step_width = len(str(max_step)) if max_step is not None else 1

    for p in series:
        step = p["step"]
        value = p["value"]
        if step is None:
            step_str = " " * step_width + "-"
        else:
            step_str = f"{step:>{step_width}}"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value_str = _format_number(value)
        else:
            value_str = str(value)
        console.print(f"  step {step_str}: {value_str}")
