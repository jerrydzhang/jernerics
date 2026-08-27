"""Plotly figure builders for the analysis page (jernerics-h5d.13).

Everything is built from plain snapshot rows with plotly alone: the
server does not depend on optuna, so the study-style views
(optimization history, parallel coordinates, slice, contour, timeline)
re-implement those shapes directly over canonical trial snapshots
(number, state, objective, flat params, timestamps) instead of calling
optuna's plotly builders.

Faceting choice: series data is long-form, so plotly-express
``facet_row`` is trivial and is used for the user-selected facet
dimension; anything harder would degrade to color-only.
"""

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import plotly.colors as pc
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .components import short_id

_STUDY_FIG_MAX_PARAMS = 8
"""Slice subplot cap per sweep; extra params are omitted, not merged."""

_PALETTE = pc.qualitative.Safe
"""One color per trial/family identity — stable across panels and modes."""

_DASHES = ("solid", "dot", "dash", "longdash", "dashdot", "longdashdot")
"""Line styles distinguishing executions within one trial."""

_PANEL_HEIGHT = 240
"""Pixel height budget per stacked panel."""


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def non_positive_count(series: list[dict[str, Any]]) -> int:
    """Plotted observations that cannot render on a log axis."""
    return sum(1 for entry in series for _step, value in entry["points"] if value <= 0)


def clipped_count(series: list[dict[str, Any]], low: float, high: float) -> int:
    """Plotted observations outside the visible custom range."""
    return sum(
        1
        for entry in series
        for _step, value in entry["points"]
        if value < low or value > high
    )


def resolve_axis(
    axis: dict[str, Any] | None, series: list[dict[str, Any]]
) -> dict[str, Any]:
    """The y-axis a panel applies: log is refused while any plotted
    observation is non-positive (points are never dropped to force it),
    custom bounds clip, auto lets plotly autorange."""
    axis = axis or {}
    non_positive = non_positive_count(series)
    log_requested = axis.get("scale") == "log"
    low, high = axis.get("min"), axis.get("max")
    custom = axis.get("range") == "custom" and low is not None and high is not None
    return {
        "scale": "log" if log_requested and non_positive == 0 else "linear",
        "range": (low, high) if custom else None,
        "log_requested": log_requested,
        "non_positive": non_positive,
        "clipped": clipped_count(series, low, high) if custom else 0,
    }


def axis_notes(resolved: dict[str, Any]) -> list[str]:
    """Header notes for one panel: why log did not apply and how many
    observations the visible range clips."""
    notes = []
    if resolved["non_positive"]:
        notes.append(
            f"log unavailable: {resolved['non_positive']} non-positive observation(s)"
        )
    if resolved["clipped"]:
        low, high = resolved["range"]
        notes.append(
            f"{resolved['clipped']} observation(s) outside [{low:g}, {high:g}]"
        )
    return notes


def series_label(series: dict[str, Any]) -> str:
    """Legend name: ``trial-shortid/exec-shortid`` (execution folded
    series name the trial alone)."""
    trial = short_id(series["trial"])
    execution = short_id(series["execution"]) if series.get("execution") else ""
    return f"{trial}/{execution}" if execution else trial


def identity_of(series: dict[str, Any], color: str | None) -> str:
    """Stable color identity: the chosen context dimension's value when
    picked, else the trial."""
    if color:
        return str((series.get("context") or {}).get(color))
    return short_id(series["trial"])


def identity_color_map(
    per_key: list[dict[str, Any]], color: str | None
) -> dict[str, str]:
    """One palette slot per identity; the same map colors every panel
    and both modes."""
    identities = sorted(
        {identity_of(series, color) for entry in per_key for series in entry["series"]}
    )
    return {
        identity: _PALETTE[index % len(_PALETTE)]
        for index, identity in enumerate(identities)
    }


def _execution_dash_factory(
    per_key: list[dict[str, Any]],
) -> Any:
    """Dash style per (trial, execution): the execution's rank among
    that trial's executions keeps retry/re-execution distinctions
    visible under a stable trial color."""
    per_trial: dict[str, set[str]] = {}
    for entry in per_key:
        for series in entry["series"]:
            if series.get("execution"):
                per_trial.setdefault(series["trial"], set()).add(series["execution"])
    ranked = {trial: sorted(executions) for trial, executions in per_trial.items()}

    def dash_of(trial: str, execution: str | None) -> str:
        if not execution:
            return "solid"
        executions = ranked.get(trial) or [execution]
        return _DASHES[executions.index(execution) % len(_DASHES)]

    return dash_of


def _scatter(
    series: dict[str, Any],
    *,
    name: str,
    color: str,
    dash: str,
    legendgroup: str,
    showlegend: bool,
) -> go.Scatter:
    return go.Scatter(
        x=[step for step, _value in series["points"]],
        y=[value for _step, value in series["points"]],
        mode="lines+markers",
        name=name,
        legendgroup=legendgroup,
        showlegend=showlegend,
        line={"color": color, "dash": dash},
        marker={"color": color},
    )


def _facet_value(series: dict[str, Any], facet: str | None) -> str:
    return str((series.get("context") or {}).get(facet)) if facet else ""


def _yaxis_kwargs(resolved: dict[str, Any]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"type": resolved["scale"]}
    if resolved["range"] is not None:
        low, high = resolved["range"]
        values = (
            (math.log10(low), math.log10(high))
            if resolved["scale"] == "log"
            else (low, high)
        )
        kwargs["range"] = list(values)
    return kwargs


def _annotate_empty(figure: go.Figure, row: int, message: str) -> None:
    suffix = "" if row == 1 else str(row)
    figure.add_annotation(
        text=message,
        xref=f"x{suffix} domain",
        yref=f"y{suffix} domain",
        x=0.5,
        y=0.5,
        showarrow=False,
    )


def stacked_figure(
    per_key: list[dict[str, Any]],
    axes: dict[str, dict[str, Any]],
    *,
    color: str | None = None,
    facet: str | None = None,
) -> go.Figure:
    """One vertically stacked subplot per key in picker order: panels
    share the step x domain (linked zoom) and keep independent y axes.
    A key with no observations keeps its panel and says so."""
    color_map = identity_color_map(per_key, color)
    dash_of = _execution_dash_factory(per_key)
    rows: list[tuple[str, str]] = []
    for entry in per_key:
        values = sorted({_facet_value(series, facet) for series in entry["series"]})
        for value in values or [""]:
            rows.append((entry["key"], value))
    figure = make_subplots(
        rows=len(rows) or 1,
        cols=1,
        shared_xaxes=True,
        subplot_titles=[
            f"{key} · {facet} = {value}" if facet else key for key, value in rows
        ]
        or None,
    )
    legend_seen: set[str] = set()
    for row_index, (key, facet_value) in enumerate(rows, start=1):
        matching = [
            series
            for entry in per_key
            if entry["key"] == key
            for series in entry["series"]
            if _facet_value(series, facet) == facet_value
        ]
        resolved = resolve_axis(axes.get(key), matching)
        for series in matching:
            identity = identity_of(series, color)
            name = series_label(series)
            figure.add_trace(
                _scatter(
                    series,
                    name=name,
                    color=color_map[identity],
                    dash=dash_of(series["trial"], series.get("execution")),
                    legendgroup=identity,
                    showlegend=identity not in legend_seen,
                ),
                row=row_index,
                col=1,
            )
            legend_seen.add(identity)
        if not matching:
            _annotate_empty(figure, row_index, "no observations under this scope")
        figure.update_yaxes(row=row_index, col=1, **_yaxis_kwargs(resolved))
    figure.update_xaxes(title="step", row=len(rows) or 1, col=1)
    figure.update_layout(
        hovermode="x unified",
        height=_PANEL_HEIGHT * (len(rows) or 1) + 90,
    )
    return figure


def overlay_figure(
    per_key: list[dict[str, Any]],
    axis: dict[str, Any] | None,
    *,
    color: str | None = None,
    facet: str | None = None,
) -> go.Figure:
    """Every selected key on ONE shared, unnormalized y axis (never a
    second axis, never rescaled data); log is refused while any plotted
    observation anywhere is non-positive."""
    pooled = [series for entry in per_key for series in entry["series"]]
    resolved = resolve_axis(axis, pooled)
    color_map = identity_color_map(per_key, color)
    dash_of = _execution_dash_factory(per_key)
    facet_values = sorted({_facet_value(series, facet) for series in pooled}) or [""]
    figure = make_subplots(
        rows=len(facet_values),
        cols=1,
        shared_xaxes=True,
        subplot_titles=[
            f"{facet} = {value}" if facet else None for value in facet_values
        ],
    )
    legend_seen: set[str] = set()
    for row_index, facet_value in enumerate(facet_values, start=1):
        for entry in per_key:
            for series in entry["series"]:
                if _facet_value(series, facet) != facet_value:
                    continue
                identity = identity_of(series, color)
                name = f"{entry['key']} · {series_label(series)}"
                figure.add_trace(
                    _scatter(
                        series,
                        name=name,
                        color=color_map[identity],
                        dash=dash_of(series["trial"], series.get("execution")),
                        legendgroup=f"{identity} · {entry['key']}",
                        showlegend=f"{identity} · {entry['key']}" not in legend_seen,
                    ),
                    row=row_index,
                    col=1,
                )
                legend_seen.add(f"{identity} · {entry['key']}")
        if not pooled:
            _annotate_empty(figure, row_index, "no observations under this scope")
        figure.update_yaxes(row=row_index, col=1, **_yaxis_kwargs(resolved))
    figure.update_xaxes(title="step", row=len(facet_values), col=1)
    figure.update_yaxes(title="value (shared, unnormalized)", row=1, col=1)
    figure.update_layout(
        hovermode="x unified",
        height=_PANEL_HEIGHT * len(facet_values) + 90,
    )
    return figure


def optimization_history(trials: list[dict[str, Any]]) -> go.Figure:
    """Trial number vs objective for trials that carry an objective."""
    done = [trial for trial in trials if trial["objective"] is not None]
    figure = go.Figure(
        go.Scatter(
            x=[trial["number"] for trial in done],
            y=[trial["objective"] for trial in done],
            mode="lines+markers",
        )
    )
    figure.update_layout(xaxis_title="trial number", yaxis_title="objective")
    return figure


def numeric_param_keys(trials: list[dict[str, Any]]) -> list[str]:
    """Param keys whose present values are all numeric (bool excluded)."""
    keys = {key for trial in trials for key in trial["params"]}
    numeric = []
    for key in sorted(keys):
        present = [trial["params"][key] for trial in trials if key in trial["params"]]
        if present and all(_is_number(value) for value in present):
            numeric.append(key)
    return numeric


def _param_dimension(key: str, values: list[Any]) -> go.parcoords.Dimension:
    present = [value for value in values if value is not None]
    if present and all(_is_number(value) for value in present):
        return go.parcoords.Dimension(
            values=[
                float(value) if _is_number(value) else math.nan for value in values
            ],
            label=key,
        )
    labels = sorted({str(value) for value in present})
    codes = {label: index for index, label in enumerate(labels)}
    return go.parcoords.Dimension(
        values=[
            codes[str(value)] if value is not None else math.nan for value in values
        ],
        label=key,
        tickvals=list(range(len(labels))),
        ticktext=labels,
    )


def parallel_coordinates(trials: list[dict[str, Any]]) -> go.Figure:
    """One dimension per param plus the objective; non-numeric params are
    category-coded with tick labels."""
    keys = sorted({key for trial in trials for key in trial["params"]})
    dimensions = [
        _param_dimension(key, [trial["params"].get(key) for trial in trials])
        for key in keys[:_STUDY_FIG_MAX_PARAMS]
    ]
    objectives = [trial["objective"] for trial in trials]
    if any(objective is not None for objective in objectives):
        dimensions.append(
            go.parcoords.Dimension(
                values=[
                    objective if objective is not None else math.nan
                    for objective in objectives
                ],
                label="objective",
            )
        )
    figure = go.Figure(go.Parcoords(dimensions=dimensions))
    if objectives and all(objective is not None for objective in objectives):
        figure.update_traces(
            line={"color": objectives, "showscale": True, "colorbar_title": "objective"}
        )
    return figure


def slice_figure(trials: list[dict[str, Any]]) -> go.Figure:
    """Objective-vs-param scatter, one subplot per param."""
    keys = sorted({key for trial in trials for key in trial["params"]})
    keys = keys[:_STUDY_FIG_MAX_PARAMS]
    if not keys:
        figure = go.Figure()
        figure.update_layout(title="no params recorded for these trials")
        return figure
    figure = make_subplots(rows=1, cols=len(keys), subplot_titles=keys)
    for column, key in enumerate(keys, start=1):
        pairs = [
            (trial["params"][key], trial["objective"])
            for trial in trials
            if key in trial["params"] and trial["objective"] is not None
        ]
        if not pairs:
            continue
        if all(_is_number(x) for x, _ in pairs):
            xs = [x for x, _ in pairs]
        else:
            labels = sorted({str(x) for x, _ in pairs})
            codes = {label: index for index, label in enumerate(labels)}
            xs = [codes[str(x)] for x, _ in pairs]
            figure.update_xaxes(
                tickvals=list(range(len(labels))),
                ticktext=labels,
                row=1,
                col=column,
            )
        figure.add_trace(
            go.Scatter(
                x=xs,
                y=[y for _, y in pairs],
                mode="markers",
                name=key,
                showlegend=False,
            ),
            row=1,
            col=column,
        )
    figure.update_layout(height=360)
    return figure


def _centers(values: list[float], bins: int) -> list[float]:
    low, high = min(values), max(values)
    if low == high:
        low, high = low - 0.5, high + 0.5
    width = (high - low) / bins
    return [low + width * (index + 0.5) for index in range(bins)]


def contour_figure(trials: list[dict[str, Any]], x_key: str, y_key: str) -> go.Figure:
    """Mean objective over a 2-D binning of two numeric params. Bins with
    no trials stay blank — no interpolation, no invented values."""
    points = [
        (
            float(trial["params"][x_key]),
            float(trial["params"][y_key]),
            trial["objective"],
        )
        for trial in trials
        if x_key in trial["params"]
        and y_key in trial["params"]
        and trial["objective"] is not None
    ]
    figure = go.Figure()
    figure.update_layout(xaxis_title=x_key, yaxis_title=y_key)
    if not points:
        figure.update_layout(
            title=f"no trials with both {x_key} and {y_key} and an objective"
        )
        return figure
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    bins = max(3, min(12, int(math.sqrt(len(points))) + 2))
    spans = {}
    for name, column in (("x", xs), ("y", ys)):
        low, high = min(column), max(column)
        if low == high:
            low, high = low - 0.5, high + 0.5
        spans[name] = (low, high)

    def bin_index(value: float, name: str) -> int:
        low, high = spans[name]
        return min(bins - 1, int((value - low) / (high - low) * bins))

    totals = [[0.0] * bins for _ in range(bins)]
    counts = [[0] * bins for _ in range(bins)]
    for x, y, objective in points:
        row, column = bin_index(y, "y"), bin_index(x, "x")
        totals[row][column] += objective
        counts[row][column] += 1
    grid = [
        [
            totals[row][column] / counts[row][column]
            if counts[row][column]
            else math.nan
            for column in range(bins)
        ]
        for row in range(bins)
    ]
    figure.add_trace(
        go.Contour(
            x=_centers(xs, bins),
            y=_centers(ys, bins),
            z=grid,
            connectgaps=False,
            colorbar={"title": "objective"},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="markers",
            marker={"color": "rgba(0,0,0,0.55)", "size": 6},
            name="trials",
            showlegend=False,
        )
    )
    return figure


def _ns_to_datetime(ns: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=ns // 1000)


def trial_timeline(trials: list[dict[str, Any]]) -> go.Figure:
    """Horizontal trial bars from created_ns to updated_ns."""
    ordered = sorted(trials, key=lambda trial: trial["number"])
    labels = [f"#{trial['number']} {short_id(trial['trial_id'])}" for trial in ordered]
    starts = [_ns_to_datetime(trial["created_ns"]).isoformat() for trial in ordered]
    durations_ms = [
        max(0, trial["updated_ns"] - trial["created_ns"]) / 1_000_000
        for trial in ordered
    ]
    figure = go.Figure(go.Bar(y=labels, base=starts, x=durations_ms, orientation="h"))
    figure.update_xaxes(type="date", title="time (UTC)")
    figure.update_yaxes(autorange="reversed")
    return figure
