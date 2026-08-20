"""Read-only Dash dashboard presentation layer for the tracking server."""

from .app import (
    DashboardContext,
    build_dash_app,
    build_dashboard_context,
    mount_dashboard,
)
from .auth import session_or_bearer_auth
from .service import DashboardService

__all__ = [
    "DashboardContext",
    "DashboardService",
    "build_dash_app",
    "build_dashboard_context",
    "mount_dashboard",
    "session_or_bearer_auth",
]
