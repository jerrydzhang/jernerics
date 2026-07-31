"""Observability layer: query tracking data and render run summaries.

``Queryable`` is the structural interface every analysis function expects.
``RemoteStore`` adapts the tracking server's ``/query`` endpoint to it, so
the CLI talks to a remote server with the same call shape tests use
against an in-process :class:`jernerics_server.store.Store`.
"""

from jernerics.observability.analysis import (
    PRIORITY_METRICS,
    Queryable,
    compute_metric_analysis,
    compute_slope,
    get_all_runs,
    get_run_diff,
    get_run_summary,
    run_exists,
)
from jernerics.observability.remote import RemoteStore
from jernerics.observability.render import (
    render_diff,
    render_runs,
    render_summary,
)

__all__ = [
    "PRIORITY_METRICS",
    "Queryable",
    "RemoteStore",
    "compute_metric_analysis",
    "compute_slope",
    "get_all_runs",
    "get_run_diff",
    "get_run_summary",
    "render_diff",
    "render_runs",
    "render_summary",
    "run_exists",
]
