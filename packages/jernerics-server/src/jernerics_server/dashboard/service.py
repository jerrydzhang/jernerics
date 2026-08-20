"""Thin read-only composition over QueryService for Dash callbacks.

DashboardService contains no SQL: every method delegates to the same
QueryService the HTTP API uses, so the dashboard can never drift from
the one SQL layer. Selections arrive from client state as plain strings
and are rebuilt into typed ``Selection`` objects here, per query call.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from jernerics_schema import Selection

from jernerics_server.queries import QueryService


@dataclass(frozen=True)
class DashboardService:
    """The only data doorway callbacks are allowed to use."""

    queries: QueryService

    def projects(self) -> list[str]:
        return self.queries.projects()

    def selection(self, project: str | None, sweep_ids: Sequence[str]) -> Selection:
        """Typed Selection for the current project plus tray sweep ids."""
        if not project:
            raise ValueError("no project selected")
        return Selection(
            project=project,
            sweeps=tuple(uuid.UUID(sweep_id) for sweep_id in sweep_ids),
        )
