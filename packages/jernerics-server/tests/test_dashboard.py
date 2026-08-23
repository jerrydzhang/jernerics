"""Server-side dashboard shell coverage (browser checks run post-merge)."""

import re
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionStartEvent,
    IngestRequest,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
)
from jernerics_server.dashboard import DashboardContext
from jernerics_server.dashboard.analysis import tray_summary
from jernerics_server.dashboard.auth import COOKIE_NAME
from jernerics_server.dashboard.callbacks import page_content
from jernerics_server.dashboard.routes import parse_route
from jernerics_server.http import create_app
from jernerics_server.store import Store

API_KEY = "secret123"
T0 = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def _build(tmp_path: Path, *, api_key: str | None = API_KEY) -> TestClient:
    store = Store(tmp_path / "dash.sqlite")
    app = create_app(
        store,
        api_key=api_key,
        artifacts_root=tmp_path / "artifacts",
        dashboard=True,
    )
    return TestClient(app, base_url="https://testserver")


def _login(client: TestClient, key: str = API_KEY) -> str:
    response = client.post(
        "/dashboard/login", data={"api_key": key}, follow_redirects=False
    )
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    return cookie.split(";", 1)[0].split("=", 1)[1]


def _ctx(client: TestClient) -> DashboardContext:
    app = client.app
    assert isinstance(app, FastAPI)
    return app.state.dashboard


def _login_url(next_value: str = "/dashboard/") -> str:
    return f"/dashboard/login?next={quote(next_value, safe='')}"


@pytest.fixture
def client(tmp_path):
    return _build(tmp_path)


@pytest.fixture
def authed(tmp_path):
    client = _build(tmp_path)
    _login(client)
    return client


class TestLogin:
    def test_wrong_key_is_401_without_cookie(self, client):
        response = client.post(
            "/dashboard/login", data={"api_key": "nope"}, follow_redirects=False
        )
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert "Invalid API key" in response.text

    def test_success_sets_exact_cookie_flags_and_redirects(self, client):
        response = client.post(
            "/dashboard/login",
            data={"api_key": API_KEY},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard/"
        cookie = response.headers["set-cookie"].lower()
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "httponly" in cookie
        assert "secure" in cookie
        assert "samesite=strict" in cookie
        assert "path=/dashboard" in cookie
        assert "max-age=43200" in cookie

    def test_success_response_echoes_no_api_key(self, client):
        response = client.post("/dashboard/login", data={"api_key": API_KEY})
        assert response.status_code == 200
        assert API_KEY not in response.text

    def test_login_page_get_renders_form(self, client):
        response = client.get("/dashboard/login")
        assert response.status_code == 200
        assert 'action="/dashboard/login"' in response.text


class TestSession:
    def test_valid_cookie_authorizes_dashboard(self, authed):
        response = authed.get("/dashboard/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_missing_cookie_redirects_to_login(self, client):
        response = client.get("/dashboard/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == _login_url()

    def test_tampered_cookie_redirects_to_login(self, client):
        client.cookies.set(COOKIE_NAME, "eyJzdWIiOiJkYXNoYm9hcmQifQ.forged")
        response = client.get("/dashboard/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == _login_url()

    def test_expired_cookie_redirects_to_login(self, tmp_path):
        client = _build(tmp_path)
        signer = _ctx(client).signer
        token = signer.sign(ttl_s=-60)
        client.cookies.set(COOKIE_NAME, token)
        response = client.get("/dashboard/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == _login_url()


class TestLogout:
    def test_logout_expires_cookie_and_redirects(self, authed):
        response = authed.post("/dashboard/logout", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard/login"
        assert "max-age=0" in response.headers["set-cookie"].lower()

    def test_dashboard_after_logout_requires_login(self, authed):
        token = authed.cookies.get(COOKIE_NAME)
        authed.post("/dashboard/logout", follow_redirects=False)
        authed.cookies.set(COOKIE_NAME, token or "")
        response = authed.get("/dashboard/", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == _login_url()


class TestLoginNext:
    """jernerics-wh2: deep URLs ride through the login round trip as the
    ``next`` parameter; only dashboard-relative targets are honored."""

    def test_deep_url_redirects_to_login_with_next(self, client):
        response = client.get("/dashboard/project/symlab", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == _login_url("/dashboard/project/symlab")

    def test_query_string_is_carried_inside_next(self, client):
        response = client.get(
            "/dashboard/analysis", params={"sel": "tok=1"}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == _login_url(
            "/dashboard/analysis?sel=tok%3D1"
        )

    def test_login_page_embeds_next_as_hidden_field(self, client):
        response = client.get(
            "/dashboard/login", params={"next": "/dashboard/sweep/abc"}
        )
        assert response.status_code == 200
        assert '<input name="next" type="hidden"' in response.text
        assert 'value="/dashboard/sweep/abc"' in response.text

    def test_login_page_drops_unsafe_next(self, client):
        response = client.get("/dashboard/login", params={"next": "https://evil.com"})
        assert response.status_code == 200
        assert 'name="next"' not in response.text

    def test_valid_key_lands_on_next_target_with_query(self, client):
        response = client.post(
            "/dashboard/login",
            data={"api_key": API_KEY, "next": "/dashboard/analysis?sel=tok=1"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard/analysis?sel=tok=1"

    def test_wrong_key_keeps_next_for_retry(self, client):
        response = client.post(
            "/dashboard/login",
            data={"api_key": "nope", "next": "/dashboard/trial/abc"},
            follow_redirects=False,
        )
        assert response.status_code == 401
        assert 'value="/dashboard/trial/abc"' in response.text

    @pytest.mark.parametrize(
        "evil",
        [
            "https://evil.com/dashboard",
            "//evil.com/dashboard",
            "/\\evil.com/dashboard",
            "http://localhost/dashboard",
            "/other/page",
            "dashboard/analysis",
            "/dashboard\nSet-Cookie: pwn=1",
            "/dashboard%0d%0aSet-Cookie:%20pwn=1",
            "/dashboard\\@evil.com",
        ],
    )
    def test_unsafe_next_falls_back_to_dashboard_root(self, client, evil):
        response = client.post(
            "/dashboard/login",
            data={"api_key": API_KEY, "next": evil},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/dashboard/"

    def test_round_trip_returns_the_deep_link(self, client):
        deep = "/dashboard/project/symlab"
        guarded = client.get(deep, follow_redirects=False)
        assert guarded.headers["location"] == _login_url(deep)

        page = client.get(guarded.headers["location"])
        assert page.status_code == 200
        assert f'value="{deep}"' in page.text

        submitted = client.post(
            "/dashboard/login",
            data={"api_key": API_KEY, "next": deep},
            follow_redirects=False,
        )
        assert submitted.status_code == 303
        assert submitted.headers["location"] == deep

        landed = client.get(deep, follow_redirects=False)
        assert landed.status_code == 200


class TestAuthSplit:
    def test_bearer_still_works_on_query_and_ingest(self, client):
        headers = {"Authorization": f"Bearer {API_KEY}"}
        assert (
            client.post("/query", json={"sql": "SELECT 1"}, headers=headers).status_code
            == 200
        )
        ingest = IngestRequest(
            protocol_version=PROTOCOL_VERSION,
            events=[
                SweepSnapshotEvent(
                    event_id=uuid.uuid4(),
                    recorded_at=T0,
                    project="proj",
                    sweep_id=uuid.uuid4(),
                    name="alpha",
                    state="running",
                ).model_dump(mode="json")
            ],
        )
        response = client.post(
            "/ingest",
            json=ingest.model_dump(mode="json"),
            headers=headers,
        )
        assert response.status_code == 200

    def test_query_rejects_session_cookie_without_bearer(self, authed):
        response = authed.post(
            "/query", json={"sql": "SELECT 1"}, headers={"Authorization": ""}
        )
        assert response.status_code == 401

    def test_artifact_get_accepts_session_cookie(self, tmp_path):
        client = _build(tmp_path)
        headers = {"Authorization": f"Bearer {API_KEY}"}
        artifact_id, payload = uuid.uuid4(), b"0123456789abcdef"
        _declare_artifact(client, artifact_id)
        put = client.put(
            f"/artifact/{artifact_id.hex}", content=payload, headers=headers
        )
        assert put.status_code == 200
        token = _login(client)
        # /artifact is the machine path: the session cookie is presented
        # directly (browsers get the same bytes via /dashboard/artifact).
        direct = client.get(
            f"/artifact/{artifact_id.hex}",
            headers={"Authorization": "", "Cookie": f"{COOKIE_NAME}={token}"},
        )
        assert direct.status_code == 200
        assert direct.content == payload
        aliased = client.get(f"/dashboard/artifact/{artifact_id.hex}")
        assert aliased.status_code == 200
        assert aliased.content == payload

    def test_artifact_put_still_bearer_only(self, authed):
        response = authed.put("/artifact/" + "0" * 32, content=b"x")
        assert response.status_code == 401


class TestDevMode:
    def test_no_api_key_requires_no_login(self, tmp_path):
        client = _build(tmp_path, api_key=None)
        response = client.get("/dashboard/")
        assert response.status_code == 200

    def test_no_api_key_login_still_issues_signed_cookie(self, tmp_path):
        client = _build(tmp_path, api_key=None)
        response = client.post(
            "/dashboard/login", data={"api_key": "anything"}, follow_redirects=False
        )
        assert response.status_code == 303
        cookie = response.headers["set-cookie"].lower()
        assert cookie.startswith(f"{COOKIE_NAME}=")
        assert "samesite=strict" in cookie


class TestSigningSecret:
    def test_secret_created_0600_and_reused_across_restarts(self, tmp_path):
        first = _build(tmp_path)
        secret_path = tmp_path / "dash.sqlite"
        secret_path = secret_path.parent / "dashboard_secret"
        assert secret_path.is_file()
        mode = stat.S_IMODE(secret_path.stat().st_mode)
        assert mode == 0o600
        token = _ctx(first).signer.sign()

        second = _build(tmp_path)
        signer = _ctx(second).signer
        assert signer.verify(token)

        third = _build(tmp_path)
        assert _ctx(third).signer.sign() != token


class TestMount:
    def test_dash_endpoints_serve_html_when_logged_in(self, authed):
        index = authed.get("/dashboard/")
        assert index.status_code == 200
        assert "react-entry-point" in index.text
        layout = authed.get("/dashboard/_dash-layout")
        assert layout.status_code == 200
        children = layout.json()["props"]["children"]
        link = children[0]["props"]
        assert link["rel"] == "icon"
        assert link["href"].endswith("/assets/favicon.svg")
        assert children[1]["props"]["id"] == "url"

    def test_deep_link_routes_return_200(self, authed):
        for path in (
            "/dashboard/sweep/0123456789abcdef0123456789abcdef",
            "/dashboard/trial/deadbeefdeadbeefdeadbeefdeadbeef",
            "/dashboard/execution/feedfacefeedfacefeedfacefeedface",
        ):
            response = authed.get(path)
            assert response.status_code == 200
            assert "react-entry-point" in response.text

    def test_unknown_path_renders_not_found_page(self, authed):
        response = authed.get("/dashboard/nope")
        assert response.status_code == 200
        assert "react-entry-point" in response.text


class TestEmptyStore:
    def test_pages_render_empty_surfaces_without_crash(self, authed, tmp_path):
        response = authed.get("/dashboard/")
        assert response.status_code == 200
        service = _ctx(authed).service
        assert service.projects() == []
        page, polls = page_content("/dashboard/", service)
        assert "No projects yet" in str(page)
        assert polls is False


class TestRoutesAndPages:
    def test_parse_route_covers_every_shell_route(self):
        assert parse_route("/dashboard").kind == "project"
        assert parse_route("/dashboard/").kind == "project"
        assert parse_route("/dashboard/sweep/abc").kind == "sweep"
        assert parse_route("/dashboard/sweep/abc").object_id == "abc"
        assert parse_route("/dashboard/trial/abc").kind == "trial"
        assert parse_route("/dashboard/execution/abc").kind == "execution"
        assert parse_route("/dashboard/whatever").kind == "not-found"

    def test_object_pages_with_unknown_ids_render_empty_surface(self, tmp_path):
        client = _build(tmp_path)
        service = _ctx(client).service
        for kind in ("sweep", "trial", "execution"):
            page, polls = page_content(f"/dashboard/{kind}/0123456789abcdef", service)
            rendered = str(page)
            assert "0123456789abcdef" in rendered
            assert "Nothing here yet" in rendered
            assert polls is False

    def test_workspace_route_parses(self):
        spec = parse_route("/dashboard/project/ops")
        assert spec.kind == "workspace"
        assert spec.object_id == "ops"

    def test_tray_summary_counts_the_unified_selection(self):
        empty = "0 sweep(s) · 0 trial(s) · 0 family/families"
        assert tray_summary(None) == empty
        assert tray_summary({"sweeps": []}) == empty
        assert tray_summary({"sweeps": ["a", "b"]}) == (
            "2 sweep(s) · 0 trial(s) · 0 family/families"
        )


class TestNoDashLeakage:
    _FORBIDDEN = re.compile(
        r"^\s*(from|import)\s+(dash|dash_ag_grid|plotly|flask)\b", re.MULTILINE
    )

    def test_server_core_modules_do_not_import_dash(self):
        root = Path(__file__).parent.parent / "src" / "jernerics_server"
        for name in ("http.py", "store.py", "ingest.py", "queries.py", "server.py"):
            source = (root / name).read_text()
            assert not self._FORBIDDEN.search(source), name

    def test_schema_and_client_packages_do_not_import_dash(self):
        repo = Path(__file__).parents[3]
        for package in ("jernerics-schema", "jernerics"):
            base = repo / "packages" / package / "src"
            for path in base.rglob("*.py"):
                assert not self._FORBIDDEN.search(path.read_text()), path

    def test_no_api_key_in_url_or_html(self, client):
        response = client.post(
            "/dashboard/login", data={"api_key": API_KEY}, follow_redirects=False
        )
        assert API_KEY not in response.headers.get("location", "")
        index = client.get("/dashboard/")
        assert API_KEY not in index.text


def _declare_artifact(client: TestClient, artifact_id: uuid.UUID) -> None:
    sweep, trial, execution = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events = [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=T0,
            project="proj",
            sweep_id=sweep,
            name=f"alpha-{sweep.hex[:8]}",
            state="running",
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=T0 + timedelta(seconds=1),
            trial_id=trial,
            sweep_id=sweep,
            number=0,
            state=TrialState.RUNNING,
            retry_root_trial_id=trial,
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=T0 + timedelta(seconds=2),
            execution_id=execution,
            trial_id=trial,
            hostname="node01",
            started_at=T0 + timedelta(seconds=2),
        ),
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=T0 + timedelta(seconds=3),
            artifact_id=artifact_id,
            trial_id=trial,
            execution_id=execution,
            key="model",
            filename="model.bin",
            content_type="application/octet-stream",
            size_bytes=16,
            source="user",
        ),
    ]
    response = client.post(
        "/ingest",
        json={
            "protocol_version": PROTOCOL_VERSION,
            "events": [event.model_dump(mode="json") for event in events],
        },
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    assert response.status_code == 200, response.text
