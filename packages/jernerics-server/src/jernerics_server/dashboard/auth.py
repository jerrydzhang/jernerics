"""Dashboard session auth: login/logout routes and the /dashboard guard.

The FastAPI app owns auth. ``DashboardAuthMiddleware`` redirects any
unauthenticated ``/dashboard`` traffic to the login page (when an api key
is configured); ``session_or_bearer_auth`` is the FastAPI dependency that
lets same-origin artifact GETs authenticate with either a bearer key or
a valid dashboard session cookie. Machine endpoints (/query, /ingest,
the domain reads) stay bearer-only. Dev mode — no api key configured —
requires no login: sessions are still issued signed, without a key check.
"""

import hmac
import secrets as secrets_module
from dataclasses import dataclass
from http.cookies import SimpleCookie
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from jernerics_server.queries import QueryService

from .routes import ROUTES_BASE
from .service import DashboardService
from .sessions import DEFAULT_TTL_S, SessionSigner

COOKIE_NAME = "jernerics_session"


@dataclass(frozen=True)
class DashboardContext:
    """Everything the dashboard needs from the server it is mounted on."""

    api_key: str | None
    queries: QueryService
    service: DashboardService
    signer: SessionSigner
    routes_base: str = ROUTES_BASE
    ttl_s: int = DEFAULT_TTL_S


def _session_cookie(
    response: RedirectResponse, ctx: DashboardContext, token: str, max_age: int
) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=max_age,
        path=ctx.routes_base,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _login_html(ctx: DashboardContext, error: bool = False) -> str:
    message = "<p class='login-error'>Invalid API key.</p>" if error else ""
    return f"""<!doctype html>
<html>
<head><title>jernerics dashboard — log in</title></head>
<body class="login-body">
  <form class="login-card" method="post" action="{ctx.routes_base}/login">
    <h1>jernerics dashboard</h1>
    {message}
    <label for="api_key">API key</label>
    <input id="api_key" name="api_key" type="password" autofocus
           autocomplete="off">
    <button type="submit">Log in</button>
  </form>
</body>
</html>"""


def register_auth_routes(app: FastAPI, ctx: DashboardContext) -> None:
    """Attach GET/POST login and POST logout under the dashboard base."""
    login_path = f"{ctx.routes_base}/login"
    logout_path = f"{ctx.routes_base}/logout"

    @app.get(login_path, response_model=None, include_in_schema=False)
    async def login_page(error: int = 0) -> HTMLResponse:
        return HTMLResponse(_login_html(ctx, error=bool(error)))

    @app.post(login_path, response_model=None, include_in_schema=False)
    async def login_submit(request: Request) -> HTMLResponse | RedirectResponse:
        body = (await request.body()).decode("utf-8")
        submitted = parse_qs(body).get("api_key", [""])[-1]
        if ctx.api_key is not None and not hmac.compare_digest(submitted, ctx.api_key):
            return HTMLResponse(_login_html(ctx, error=True), status_code=401)
        response = RedirectResponse(f"{ctx.routes_base}/", status_code=303)
        _session_cookie(response, ctx, ctx.signer.sign(ctx.ttl_s), ctx.ttl_s)
        return response

    @app.post(logout_path, response_model=None, include_in_schema=False)
    async def logout(request: Request) -> RedirectResponse:
        response = RedirectResponse(login_path, status_code=303)
        token = request.cookies.get(COOKIE_NAME)
        if token:
            ctx.signer.revoke(token)
        _session_cookie(response, ctx, token or "", 0)
        return response


def session_or_bearer_auth(ctx: DashboardContext):
    """FastAPI dependency: bearer key OR valid dashboard session cookie."""

    def check(request: Request) -> None:
        if ctx.api_key is not None:
            authorization = request.headers.get("authorization", "")
            if authorization.startswith("Bearer ") and secrets_module.compare_digest(
                authorization[7:], ctx.api_key
            ):
                return
        token = request.cookies.get(COOKIE_NAME)
        if token and ctx.signer.verify(token):
            return
        raise HTTPException(status_code=401, detail="invalid credentials")

    return check


class DashboardAuthMiddleware:
    """Redirects unauthenticated /dashboard traffic to the login page."""

    def __init__(self, app: ASGIApp, ctx: DashboardContext) -> None:
        self.app = app
        self.ctx = ctx

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            base = self.ctx.routes_base
            login_path = f"{base}/login"
            guarded = (
                self.ctx.api_key is not None
                and (path == base or path.startswith(f"{base}/"))
                and path != login_path
            )
            if guarded and not self._authorized(scope):
                await RedirectResponse(login_path, status_code=303)(
                    scope, receive, send
                )
                return
        await self.app(scope, receive, send)

    def _authorized(self, scope: Scope) -> bool:
        headers = dict(scope.get("headers", []))
        if self.ctx.api_key is not None:
            authorization = headers.get(b"authorization", b"").decode("latin-1")
            if authorization.startswith("Bearer ") and secrets_module.compare_digest(
                authorization[7:], self.ctx.api_key
            ):
                return True
        cookies = SimpleCookie()
        cookies.load(headers.get(b"cookie", b"").decode("latin-1"))
        morsel = cookies.get(COOKIE_NAME)
        return morsel is not None and self.ctx.signer.verify(morsel.value)
