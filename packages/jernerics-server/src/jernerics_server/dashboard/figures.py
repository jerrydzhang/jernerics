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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import plotly.colors as pc
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .components import short_id

_DASHES = ("solid", "dot", "dash", "longdash", "dashdot", "longdashdot")
"""Line styles distinguishing executions within one trial."""

_PALETTE = pc.qualitative.Safe
"""One color per trial/color-group identity — stable across panels and modes."""

_SEQUENTIAL = pc.sequential.Viridis
"""Sequential palette for equal-width numeric color ranges."""

_UIREVISION = "analysis-series"
"""Stable Plotly revision: re-renders preserve user zoom and axes."""

_DENSE_SERIES_LIMIT = 100
"""Raw series per panel above which the density warning shows."""

_STUDY_FIG_MAX_PARAMS = 8
"""Slice subplot cap per sweep; extra params are omitted, not merged."""

MAX_PARAM_DIMS = _STUDY_FIG_MAX_PARAMS
"""Public cap for params → outcome parallel-coordinate dimensions."""

SERIES_POINT_CAP = 1500
"""Plotted points per series above which the sweep-series view thins:
stride sampling keeps the first and last observation, so shape and
finals stay exact while poll payloads stay bounded (jernerics-1r00)."""

_PANEL_HEIGHT = 360
"""Height of one series subplot row."""

_RANGE_BINS = 8
"""Equal-width ranges for a numeric param with more than this many values."""

_MISSING_LABEL = "missing"
_MISSING_COLOR = "#7f7f7f"
_PARAM_COLOR_PREFIX = "param:"

_LEGEND = {
    "font": {"size": 10},
    "itemsizing": "constant",
    "itemwidth": 30,
    "x": 1,
    "y": 0.5,
    "yanchor": "middle",
    "bgcolor": "rgba(255, 255, 255, 0.6)",
}
"""On-chart legend style: compact, vertically centered at the right so
the hover modebar never sits on the first entry (jernerics-bt9)."""

_STUDY_FIG_HEIGHT = 240
"""Height of one Optuna study figure — compact, the grid pairs them."""

_STUDY_FIG_MARGIN = {"l": 48, "r": 16, "t": 30, "b": 36}
"""Compact margins; plotly's 80px defaults would eat a 240px figure."""
_PARCOORDS_MARGIN = {**_STUDY_FIG_MARGIN, "t": 60}
"""Parcoords draws rotated dimension labels above the plot box; the
compact top margin clips them (jernerics-3yam)."""


def _series_height(rows: int, legend_entries: int) -> int:
    """Panel-row height plus a capped legend allowance: a legend taller
    than the plot area grows the figure instead of clipping (jernerics-bt9)."""
    plot_area = _PANEL_HEIGHT * rows - 120
    return _PANEL_HEIGHT * rows + 90 + max(0, min(660, 30 * legend_entries - plot_area))


def downsample_points(
    points: list[tuple[int, float]], cap: int = SERIES_POINT_CAP
) -> list[tuple[int, float]]:
    """Stride-thinned (step, value) points under ``cap``; the first and
    last observations always survive, so finals and end shape are exact."""
    if len(points) <= cap:
        return points
    stride = max(1, math.ceil((len(points) - 1) / (cap - 1)))
    thinned = points[::stride]
    if thinned[-1] != points[-1]:
        thinned.append(points[-1])
    return thinned


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


def empty_figure() -> go.Figure:
    """A blank figure with the stable uirevision for no-figure states."""
    return go.Figure(layout={"uirevision": _UIREVISION})


def percentile(values: list[float], q: int) -> float:
    """Linear-interpolation percentile (numpy's default method) over
    observed values only; ``q`` is 0-100."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * (q / 100)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def median_iqr_summary(
    per_key: list[dict[str, Any]], color: str | None = None, facet: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Per key and explicit (facet, color-group) group: median, q25,
    q75, and contributing count at each OBSERVED step — absent steps stay
    absent, values are never interpolated. Without a color choice every
    series of a key forms one group."""
    grouped_all = color is not None
    grouping = (
        color_grouping(
            [series for entry in per_key for series in entry["series"]], color
        )
        if grouped_all
        else None
    )
    summaries: dict[str, list[dict[str, Any]]] = {}
    for entry in per_key:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for series in entry["series"]:
            identity = identity_of(series, grouping) if grouping else "all trials"
            grouped.setdefault((_facet_value(series, facet), identity), []).append(
                series
            )
        rows = []
        for (facet_value, identity), members in sorted(grouped.items()):
            by_step: dict[int, list[float]] = {}
            for series in members:
                for step, value in series["points"]:
                    by_step.setdefault(step, []).append(value)
            steps = sorted(by_step)
            rows.append(
                {
                    "facet": facet_value,
                    "identity": identity,
                    "series_count": len(members),
                    "steps": steps,
                    "median": [percentile(by_step[step], 50) for step in steps],
                    "q25": [percentile(by_step[step], 25) for step in steps],
                    "q75": [percentile(by_step[step], 75) for step in steps],
                    "counts": [len(by_step[step]) for step in steps],
                }
            )
        summaries[entry["key"]] = rows
    return summaries


def count_note(series_count: int, display: str) -> list[str]:
    """Raw-series count note plus the line-density warning above 100 in
    All-raw mode (never a silent sample or mode switch)."""
    notes = [f"{series_count} series"]
    if display == "all" and series_count > _DENSE_SERIES_LIMIT:
        notes.append(
            f"line density: {series_count} series may render slowly — "
            "consider Highlighted only or Median + IQR"
        )
    return notes


def parse_color(color: str | None) -> tuple[str | None, str | None]:
    """(kind, key) of a Color-by token: ``("param", key)``,
    ``("context", key)``, or ``(None, None)`` for no choice."""
    if not color:
        return None, None
    if color.startswith(_PARAM_COLOR_PREFIX):
        return "param", color[len(_PARAM_COLOR_PREFIX) :]
    return "context", color


def value_text(value: Any) -> str:
    """Deterministic text for one sampled/context value in labels and cells."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int | float):
        return f"{value:g}"
    return str(value)


def _context_label(value: Any) -> str:
    return _MISSING_LABEL if value is None else str(value)


def _param_label(value: Any, bins: dict | None) -> str:
    if value is None:
        return _MISSING_LABEL
    if bins is not None and _is_number(value):
        slot = min(int((value - bins["low"]) / bins["width"]), _RANGE_BINS - 1)
        return bins["labels"][max(slot, 0)]
    return value_text(value)


def _numeric_bins(values: list[float]) -> dict | None:
    """Eight labeled equal-width range slots over the observed extent;
    ``None`` when the values do not need ranges (eight or fewer)."""
    distinct = sorted(set(values))
    if len(distinct) <= _RANGE_BINS:
        return None
    low = distinct[0]
    width = (distinct[-1] - low) / _RANGE_BINS or 1.0
    return {
        "low": low,
        "width": width,
        "labels": [
            f"{low + index * width:g}-{low + (index + 1) * width:g}"
            for index in range(_RANGE_BINS)
        ],
    }


def color_grouping(records: list[dict[str, Any]], color: str | None = None) -> dict:
    """Color groups over records (dicts with ``trial``/``params``/
    ``context``): one palette slot per categorical label, eight labeled
    equal-width sequential-palette ranges for a numeric param with more
    than eight distinct values, gray for missing. The same map colors
    raw traces, summaries, facets, legends, and browser swatches."""
    kind, key = parse_color(color)
    bins: dict | None = None
    if kind == "context":
        labels = sorted(
            {
                _context_label((record.get("context") or {}).get(key))
                for record in records
            }
        )
    elif kind == "param":
        values = [
            value
            for record in records
            if (value := (record.get("params") or {}).get(key)) is not None
        ]
        numeric = [float(value) for value in values if _is_number(value)]
        bins = _numeric_bins(numeric) if len(numeric) == len(values) else None
        labels = (
            bins["labels"]
            if bins is not None
            else sorted(
                {
                    _param_label((record.get("params") or {}).get(key), None)
                    for record in records
                }
            )
        )
    else:
        key = None
        labels = sorted({short_id(record["trial"]) for record in records})
    if bins is not None:
        colors = dict(
            zip(
                bins["labels"],
                pc.sample_colorscale(_SEQUENTIAL, _RANGE_BINS),
                strict=True,
            )
        )
    else:
        colors = {
            label: (
                _MISSING_COLOR
                if label == _MISSING_LABEL
                else _PALETTE[index % len(_PALETTE)]
            )
            for index, label in enumerate(labels)
        }
    colors.setdefault(_MISSING_LABEL, _MISSING_COLOR)
    return {"kind": kind, "key": key, "labels": labels, "colors": colors, "bins": bins}


def identity_of(record: dict[str, Any], grouping: dict) -> str:
    """The record's color-group label under one :func:`color_grouping` map."""
    kind, key = grouping["kind"], grouping["key"]
    if kind == "context":
        return _context_label((record.get("context") or {}).get(key))
    if kind == "param":
        return _param_label((record.get("params") or {}).get(key), grouping["bins"])
    return short_id(record["trial"])


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


def hover_identity(series: dict[str, Any]) -> str:
    """Compact hover identity: ``sweep/trial/execution`` short ids — the
    sweep appears only when the builder attached one."""
    bits = []
    if series.get("sweep"):
        bits.append(short_id(series["sweep"]))
    bits.append(short_id(series["trial"]))
    if series.get("execution"):
        bits.append(short_id(series["execution"]))
    return "/".join(bits)


def trace_hovertemplate(series: dict[str, Any]) -> str:
    """Hover text: identity, step/value, and the varying configuration."""
    lines = [f"{hover_identity(series)} · value %{{y:.6g}} @ step %{{x}}"]
    if series.get("config"):
        lines.append(series["config"])
    return "<br>".join(lines) + "<extra></extra>"


def _scatter(
    series: dict[str, Any],
    *,
    name: str,
    color: str,
    dash: str,
    legendgroup: str,
    showlegend: bool,
    opacity: float | None = None,
) -> go.Scatter:
    kwargs: dict[str, Any] = {"opacity": opacity} if opacity is not None else {}
    return go.Scatter(
        x=[step for step, _value in series["points"]],
        y=[value for _step, value in series["points"]],
        mode="lines+markers",
        name=name,
        legendgroup=legendgroup,
        showlegend=showlegend,
        line={"color": color, "dash": dash},
        marker={"color": color},
        customdata=[series["trial"]] * len(series["points"]),
        hovertemplate=trace_hovertemplate(series),
        **kwargs,
    )


def _rgba(color: str, alpha: float) -> str:
    """``rgba(...)`` from a plotly ``rgb(...)`` or ``#rrggbb`` color."""
    if color.startswith("rgb("):
        red, green, blue = (int(part) for part in color[4:-1].split(","))
    else:
        digits = color.lstrip("#")
        red, green, blue = (int(digits[i : i + 2], 16) for i in (0, 2, 4))
    return f"rgba({red}, {green}, {blue}, {alpha})"


def _add_summary_traces(
    figure: go.Figure,
    group: dict[str, Any],
    *,
    color: str,
    row: int,
    name_prefix: str = "",
    legend_seen: set[str],
) -> None:
    """One median line with a q25-q75 band; the legend carries the
    contributing series count, the hover the per-step count."""
    legendgroup = f"summary-{group['identity']}-{name_prefix}"
    name = f"{name_prefix}{group['identity']} · median ({group['series_count']} series)"
    showlegend = legendgroup not in legend_seen
    figure.add_trace(
        go.Scatter(
            x=group["steps"],
            y=group["q25"],
            line={"width": 0},
            hoverinfo="skip",
            showlegend=False,
            legendgroup=legendgroup,
        ),
        row=row,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=group["steps"],
            y=group["q75"],
            fill="tonexty",
            fillcolor=_rgba(color, 0.18),
            line={"width": 0},
            hoverinfo="skip",
            showlegend=False,
            legendgroup=legendgroup,
        ),
        row=row,
        col=1,
    )
    figure.add_trace(
        go.Scatter(
            x=group["steps"],
            y=group["median"],
            mode="lines+markers",
            name=name,
            legendgroup=legendgroup,
            showlegend=showlegend,
            line={"color": color},
            customdata=group["counts"],
            hovertemplate=(
                f"{group['identity']}<br>step %{{x}} · median %{{y:.4g}}"
                "<br>%{customdata} contributing series<extra></extra>"
            ),
        ),
        row=row,
        col=1,
    )
    legend_seen.add(legendgroup)


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
    display: str = "all",
    highlighted: Sequence[str] = (),
    grouping: dict | None = None,
) -> go.Figure:
    """One vertically stacked subplot per key in picker order: panels
    share the step x domain (linked zoom) and keep independent y axes.
    ``display`` picks raw traces (all, dimmed to ``highlighted`` picks),
    highlighted-only traces, or per-group median + IQR summaries. A key
    with no observations keeps its panel and says so. The on-chart
    legend carries one entry per color group — or per trial when no
    color choice was made. ``grouping`` overrides the grouping derived
    from ``color`` (pre-filter, so filtering never reshuffles colors)."""
    pooled = [series for entry in per_key for series in entry["series"]]
    grouping = grouping or color_grouping(pooled, color)
    colors = grouping["colors"]
    semantic = grouping["kind"] is not None
    dash_of = _execution_dash_factory(per_key)
    picks = set(highlighted)
    summaries = (
        median_iqr_summary(per_key, color, facet) if display == "median_iqr" else None
    )
    rows: list[tuple[str, str]] = []
    for entry in per_key:
        if summaries is not None:
            values = sorted({group["facet"] for group in summaries[entry["key"]]})
        else:
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
        if summaries is not None:
            for group in summaries[key]:
                if group["facet"] != facet_value:
                    continue
                _add_summary_traces(
                    figure,
                    group,
                    color=colors.get(group["identity"], _PALETTE[0]),
                    row=row_index,
                    legend_seen=legend_seen,
                )
        else:
            for series in matching:
                if display == "highlighted" and series["trial"] not in picks:
                    continue
                identity = identity_of(series, grouping)
                dimmed = bool(picks) and series["trial"] not in picks
                figure.add_trace(
                    _scatter(
                        series,
                        name=identity if semantic else series_label(series),
                        color=colors.get(identity, _PALETTE[0]),
                        dash=dash_of(series["trial"], series.get("execution")),
                        legendgroup=identity,
                        showlegend=identity not in legend_seen,
                        opacity=0.25 if dimmed else None,
                    ),
                    row=row_index,
                    col=1,
                )
                legend_seen.add(identity)
        if (summaries is not None and not summaries[key]) or not matching:
            _annotate_empty(figure, row_index, "no observations under this scope")
        resolved = resolve_axis(axes.get(key), matching)
        figure.update_yaxes(row=row_index, col=1, **_yaxis_kwargs(resolved))
    figure.update_xaxes(title="step", row=len(rows) or 1, col=1)
    figure.update_layout(
        hovermode="x unified",
        height=_series_height(len(rows) or 1, len(legend_seen)),
        uirevision=_UIREVISION,
        showlegend=True,
        legend=_LEGEND,
    )
    return figure


def overlay_figure(
    per_key: list[dict[str, Any]],
    axis: dict[str, Any] | None,
    *,
    color: str | None = None,
    facet: str | None = None,
    display: str = "all",
    highlighted: Sequence[str] = (),
    grouping: dict | None = None,
) -> go.Figure:
    """Every selected key on ONE shared, unnormalized y axis (never a
    second axis, never rescaled data); log is refused while any plotted
    observation anywhere is non-positive. ``display`` mirrors
    :func:`stacked_figure`; the legend carries one entry per color
    group — or per trial across keys when no color choice was made —
    and ``grouping`` overrides the one derived from ``color``."""
    pooled = [series for entry in per_key for series in entry["series"]]
    resolved = resolve_axis(axis, pooled)
    grouping = grouping or color_grouping(pooled, color)
    colors = grouping["colors"]
    semantic = grouping["kind"] is not None
    dash_of = _execution_dash_factory(per_key)
    picks = set(highlighted)
    summaries = (
        median_iqr_summary(per_key, color, facet) if display == "median_iqr" else None
    )
    if summaries is not None:
        facet_values = sorted(
            {group["facet"] for groups in summaries.values() for group in groups}
        ) or [""]
    else:
        facet_values = sorted({_facet_value(series, facet) for series in pooled}) or [
            ""
        ]
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
            if summaries is not None:
                for group in summaries[entry["key"]]:
                    if group["facet"] != facet_value:
                        continue
                    _add_summary_traces(
                        figure,
                        group,
                        color=colors.get(group["identity"], _PALETTE[0]),
                        row=row_index,
                        name_prefix=f"{entry['key']} · ",
                        legend_seen=legend_seen,
                    )
                continue
            for series in entry["series"]:
                if _facet_value(series, facet) != facet_value:
                    continue
                if display == "highlighted" and series["trial"] not in picks:
                    continue
                identity = identity_of(series, grouping)
                dimmed = bool(picks) and series["trial"] not in picks
                group_key = f"{identity} · {entry['key']}"
                dedup_key = group_key if semantic else identity
                figure.add_trace(
                    _scatter(
                        series,
                        name=(
                            f"{entry['key']} · {identity}"
                            if semantic
                            else f"{entry['key']} · {series_label(series)}"
                        ),
                        color=colors.get(identity, _PALETTE[0]),
                        dash=dash_of(series["trial"], series.get("execution")),
                        legendgroup=group_key,
                        showlegend=dedup_key not in legend_seen,
                        opacity=0.25 if dimmed else None,
                    ),
                    row=row_index,
                    col=1,
                )
                legend_seen.add(dedup_key)
        if not pooled:
            _annotate_empty(figure, row_index, "no observations under this scope")
        figure.update_yaxes(row=row_index, col=1, **_yaxis_kwargs(resolved))
    figure.update_xaxes(title="step", row=len(facet_values), col=1)
    figure.update_yaxes(title="value (shared, unnormalized)", row=1, col=1)
    figure.update_layout(
        hovermode="x unified",
        height=_series_height(len(facet_values), len(legend_seen)),
        uirevision=_UIREVISION,
        showlegend=True,
        legend=_LEGEND,
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
    figure.update_layout(
        xaxis_title="trial number",
        yaxis_title="objective",
        height=_STUDY_FIG_HEIGHT,
        margin=_STUDY_FIG_MARGIN,
    )
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
    figure.update_layout(
        height=_STUDY_FIG_HEIGHT,
        margin={**_PARCOORDS_MARGIN, "l": 44, "r": 44},
    )
    return figure


_POINTS_FIG_HEIGHT = 380


def padded_range(values: list[float]) -> list[float]:
    """Full-data range with 5% headroom; a constant dimension spans
    ±0.5. Ranges always cover every line, so brushing can never
    rescale an axis."""
    present = [value for value in values if not math.isnan(value)]
    if not present:
        return [0.0, 1.0]
    low, high = min(present), max(present)
    if low == high:
        low, high = low - 0.5, high + 0.5
    pad = (high - low) * 0.05
    return [low - pad, high + pad]


def points_parcoords(
    dims: list[dict[str, Any]], keep: Sequence[str] | None = None
) -> go.Figure:
    """Params → outcome parallel coordinates over the trials×final-scalars
    set. Every line stays plotted, each dimension carries its explicit
    full-data range (brushing fades natively, axes never rescale), and
    the line colors are the line order so the client restyles a
    selection's colors without re-reading the data. ``keep`` limits the
    plotted dimensions to those labels; ``None`` keeps every dimension."""
    plotted = [dim for dim in dims if keep is None or dim["label"] in keep]
    dimensions = [
        go.parcoords.Dimension(
            values=dim["values"],
            label=dim["label"],
            range=dim["range"],
            **(
                {"tickvals": dim["tickvals"], "ticktext": dim["ticktext"]}
                if dim.get("tickvals") is not None
                else {}
            ),
        )
        for dim in plotted
    ]
    figure = go.Figure(
        go.Parcoords(
            dimensions=dimensions,
            line={
                "color": list(range(len(plotted[0]["values"]) if plotted else 0)),
                "colorscale": "Viridis",
                "showscale": False,
            },
        )
    )
    figure.update_layout(
        height=_POINTS_FIG_HEIGHT,
        margin={**_PARCOORDS_MARGIN, "l": 60, "r": 60},
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
    figure.update_layout(height=_STUDY_FIG_HEIGHT, margin=_STUDY_FIG_MARGIN)
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
    figure.update_layout(
        xaxis_title=x_key,
        yaxis_title=y_key,
        height=_STUDY_FIG_HEIGHT,
        margin={**_STUDY_FIG_MARGIN, "r": 70},
    )
    if not points:
        figure.update_layout(
            title=f"no trials with both {x_key} and {y_key} and an objective",
            margin=_STUDY_FIG_MARGIN,
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
    figure.update_layout(
        # One bar per trial, capped: big sweeps grow toward, never past,
        # two compact figures (jernerics-bt9).
        height=max(
            _STUDY_FIG_HEIGHT, min(_STUDY_FIG_HEIGHT * 2, 90 + 22 * len(ordered))
        ),
        margin={**_STUDY_FIG_MARGIN, "l": 110},
    )
    return figure


def compare_heatmap(
    row_labels: list[str], column_labels: list[str], values: list[list[float | None]]
) -> go.Figure:
    """Factor-value x sampled-signature outcome grid; a signature a
    member never completed stays blank — no interpolation."""
    figure = go.Figure()
    if not row_labels or not column_labels:
        figure.update_layout(
            title="no sampled signature is common to every analyzable member",
            margin=_STUDY_FIG_MARGIN,
        )
        return figure
    figure.add_trace(
        go.Heatmap(
            z=[
                [math.nan if value is None else value for value in row]
                for row in values
            ],
            x=column_labels,
            y=row_labels,
            colorbar={"title": "outcome"},
            hovertemplate="%{y} · %{x}<br>outcome %{z:.4g}<extra></extra>",
        )
    )
    figure.update_layout(
        margin={"l": 60, "r": 12, "t": 8, "b": 60},
    )
    return figure


def compare_ranking(
    labels: list[str], medians: list[float], matched: list[int]
) -> go.Figure:
    """Per-factor-value median over the common signatures' outcomes,
    the bar text naming how many signatures each median pools."""
    figure = go.Figure()
    if not labels:
        figure.update_layout(
            title="no common signatures to rank",
            margin=_STUDY_FIG_MARGIN,
        )
        return figure
    figure.add_trace(
        go.Bar(
            x=labels,
            y=medians,
            text=[f"{count} matched" for count in matched],
            marker={"color": "#2563eb"},
            hovertemplate="%{x} · median %{y:.4g}<extra></extra>",
        )
    )
    figure.update_layout(margin={"l": 60, "r": 12, "t": 8, "b": 32})
    return figure
