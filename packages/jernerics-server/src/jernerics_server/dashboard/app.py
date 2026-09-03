"""Dash app assembly and mounting onto the FastAPI tracking server.

FastAPI keeps owning APIs, artifacts, and auth; the Dash WSGI app is
mounted under ``/dashboard`` as a read-only presentation layer. Its
callbacks read exclusively through the shared QueryService (wrapped by
DashboardService) — there is no second SQL layer.
"""

from pathlib import Path
from typing import cast

import dash
from a2wsgi.wsgi import WSGIMiddleware
from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.types import ASGIApp

from jernerics_server.queries import QueryService
from jernerics_server.store import Store

from . import callbacks, layout
from .auth import DashboardAuthMiddleware, DashboardContext, register_auth_routes
from .service import DashboardService
from .sessions import SessionSigner, load_or_create_secret

_PAGE_STYLESHEET = "page.css"


def register_page_stylesheet(app: FastAPI, ctx: DashboardContext) -> None:
    """Serve the new-shell stylesheet from the HTTP layer: Dash ignores
    the file so legacy page HTML stays byte-identical, and new pages
    mount the link themselves."""

    @app.get(f"{ctx.routes_base}/assets/{_PAGE_STYLESHEET}")
    def page_stylesheet() -> FileResponse:
        return FileResponse(
            Path(__file__).parent / "assets" / _PAGE_STYLESHEET,
            media_type="text/css",
        )


def build_dash_app(ctx: DashboardContext) -> dash.Dash:
    """The read-only presentation app, served under ``ctx.routes_base``."""
    app = dash.Dash(
        __name__,
        requests_pathname_prefix=f"{ctx.routes_base}/",
        routes_pathname_prefix="/",
        suppress_callback_exceptions=True,
        title="jernerics dashboard",
        assets_ignore=rf"^{_PAGE_STYLESHEET}$",
    )
    app.layout = layout.shell()
    callbacks.register_callbacks(app, ctx.service)
    # Deep links serve the SPA shell; dcc.Location plus the router
    # callback render the focused page client-side.
    app.server.add_url_rule(
        "/<path:_deep_link>", endpoint="dashboard_deep_link", view_func=app.index
    )
    return app


def build_dashboard_context(
    store: Store,
    *,
    queries: QueryService,
    api_key: str | None,
) -> DashboardContext:
    """Server context injected into the Dash app (QueryService-backed)."""
    return DashboardContext(
        api_key=api_key,
        queries=queries,
        service=DashboardService(queries, store),
        signer=SessionSigner(load_or_create_secret(store.path.parent)),
    )


def mount_dashboard(app: FastAPI, ctx: DashboardContext) -> DashboardContext:
    """Mount the authenticated read-only dashboard at ``ctx.routes_base``."""
    register_auth_routes(app, ctx)
    register_page_stylesheet(app, ctx)
    if ctx.api_key is not None:
        app.add_middleware(DashboardAuthMiddleware, ctx=ctx)
    # a2wsgi types the ASGI scope as a bare MutableMapping; the app is ASGI.
    app.mount(
        ctx.routes_base, cast(ASGIApp, WSGIMiddleware(build_dash_app(ctx).server))
    )
    app.state.dashboard = ctx
    return ctx
