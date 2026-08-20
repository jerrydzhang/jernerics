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


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def overlay_figure(
    rows: list[dict[str, Any]],
    *,
    key: str | None,
    color: str | None = None,
    facet: str | None = None,
) -> go.Figure:
    """Line chart over step: exactly one trace per series id, so the
    legend names every (trial, execution) pair as
    ``trial-shortid/exec-shortid``.

    A chosen context dimension re-keys color (one legend entry per
    dimension value, series keep their own traces); a facet dimension
    splits subplot rows — one row per dimension value, via
    ``make_subplots`` (plotly-express ``facet_row`` would need pandas,
    which this server does not ship). A series whose context varies
    mid-run facets by its first point's value.
    """
    if not rows:
        figure = go.Figure()
        figure.update_layout(
            title=f"no numeric values for {key or '(no key)'}",
            xaxis_title="step",
            yaxis_title=key or "value",
        )
        return figure
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["series"], []).append(row)
    palette = pc.qualitative.Safe
    color_values = sorted({str(row.get(color)) for row in rows}) if color else []
    color_of = {
        value: palette[index % len(palette)] for index, value in enumerate(color_values)
    }
    facet_values = sorted({str(row.get(facet)) for row in rows}) if facet else []
    row_of = {value: index + 1 for index, value in enumerate(facet_values)}
    figure = (
        make_subplots(
            rows=len(facet_values) or 1,
            cols=1,
            subplot_titles=[f"{facet} = {value}" for value in facet_values] or None,
        )
        if facet
        else go.Figure()
    )
    legend_seen: set[str] = set()
    for series in sorted(grouped):
        points = grouped[series]
        trace = go.Scatter(
            x=[point["step"] for point in points],
            y=[point["value"] for point in points],
            mode="lines+markers",
            name=series,
        )
        if color:
            value = str(points[0].get(color))
            trace.update(
                line={"color": color_of[value]},
                marker={"color": color_of[value]},
                legendgroup=value,
                showlegend=value not in legend_seen,
            )
            legend_seen.add(value)
        if facet:
            figure.add_trace(trace, row=row_of[str(points[0].get(facet))], col=1)
        else:
            figure.add_trace(trace)
    figure.update_xaxes(title="step")
    figure.update_yaxes(title=key)
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
