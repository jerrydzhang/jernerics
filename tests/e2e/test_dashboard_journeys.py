"""Journey-level acceptance for the dashboard link graph.

The page suites prove each view renders given facts; this module proves
the front doors between them exist. One finished sweep — its single
trial completed, execution ended, and artifact received — is driven
through the real pipeline (deploy submission events, a runner trial
with live streaming, post-hook reconciliation and blob upload) into a
fresh authenticated server with the dashboard mounted. The link-graph
walk then starts at the landing page and follows ONLY links harvested
from rendered layouts: ``html.A`` hrefs, AG Grid markdown cells, and
the artifact grid's row-click navigation. A mounted smoke proves
browser login exchanges the API key for a session cookie and
``/dashboard/`` renders over real TCP.
"""

import re
import socket
import threading
import time
from collections import deque
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urljoin

import httpx
import optuna
import pytest
import uvicorn
from dash import html
from dash.development.base_component import Component
from dash_ag_grid import AgGrid
from jernerics.backend.host import LocalHost
from jernerics.backend.models import JobSubmission, SubmitResult, SweepSubmission
from jernerics.backend.submission import (
    build_submission_events,
    write_submission_events,
)
from jernerics.post_hook import PipelineResult, run_pipeline
from jernerics.retry import RetryContext
from jernerics.runner import run_trial
from jernerics.tracking.batch_sync import ship_events_file
from jernerics_server.dashboard import workspace
from jernerics_server.dashboard.app import build_dash_app
from jernerics_server.dashboard.artifacts import raw_href, viewer_href
from jernerics_server.dashboard.auth import COOKIE_NAME
from jernerics_server.dashboard.callbacks import page_content
from jernerics_server.dashboard.components import short_id
from jernerics_server.dashboard.layout import shell
from jernerics_server.dashboard.routes import ROUTES_BASE, parse_route
from jernerics_server.http import create_app
from jernerics_server.store import Store
from optuna.storages.journal import JournalFileBackend, JournalStorage

optuna.logging.set_verbosity(optuna.logging.WARNING)

API_KEY = "journeys-secret"
PROJECT = "journeys"
SWEEP = "dashboard-journeys-e2e"
LANDING = f"{ROUTES_BASE}/"

TRIAL_SCRIPT = """\
import os
import sys
from pathlib import Path
from uuid import UUID

from jernerics import trial_config, trial_tracker
from jernerics.tracking.tracker import JsonlTracker

config = trial_config()
tracker = trial_tracker()
number = os.environ["JERNERICS_TRIAL_NUMBER"]
root = Path(os.environ["JERNERICS_TRACKING_DIR"])

tracker.log_param("batch_size", 32)
for step in range(3):
    tracker.log_value("loss", 1.0 - 0.25 * step, step=step)

raw = JsonlTracker(
    root / "events" / f"{number}.jsonl",
    UUID(os.environ["JERNERICS_TRIAL_ID"]),
    UUID(os.environ["JERNERICS_EXECUTION_ID"]),
)
raw.set_progress(3, 3, "steps")

out = root.parent / "artifacts-out"
out.mkdir(exist_ok=True)
model = out / f"model-{number}.txt"
model.write_text(f"journeys-model-{number}")
tracker.log_artifact("model", str(model))

print(f"trial {number} stdout")
print(f"trial {number} stderr", file=sys.stderr)

tracker.finish({"loss": 0.25})
"""

PYPROJECT_SOURCE = """\
[project]
name = "proj"
version = "0.1.0"

[tool.jernerics.backends.slurm]
type = "slurm"
grace_period_s = 0
stale_after_s = 0
fast_fail_threshold_s = 0
max_retries = 2
"""

CONFIG_SOURCE = """\
base = {"batch_note": "e2e"}
n_trials = 1

def search_space(trial):
    return {
        "rate": trial.suggest_float("rate", 0.1, 0.9),
        "seed": trial.suggest_int("seed", 1, 5),
        "mode": trial.suggest_categorical("mode", ["a", "b"]),
    }

def objective(results):
    return results["loss"]
"""

_ARTIFACT_ROW_ID = {"function": "jernericsArtifactRowId(params)"}
_MARKDOWN_HREF = re.compile(r"\]\(([^)]+)\)")


def _rows(db_path: Path, sql: str, params: list | None = None) -> list[tuple]:
    with Store(db_path) as store:
        return store.query(sql, params)[1]


def _start_server(tmp_path: Path) -> tuple[object, str]:
    """Authenticated server with dashboard mounted, on a random port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    store = Store(tmp_path / "server.sqlite")
    app = create_app(
        store,
        api_key=API_KEY,
        artifacts_root=tmp_path / "artifacts",
        dashboard=True,
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return app, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    """Drive one completed sweep through the real pipeline into a fresh
    authenticated server: deploy events ship with the bearer key, the
    runner trial streams live (JERNERICS_API_KEY), and the post-hook
    reconciles the journal and uploads the pending blobs."""
    tmp_path = tmp_path_factory.mktemp("dashboard-journeys")
    app, base_url = _start_server(tmp_path)

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(PYPROJECT_SOURCE)
    config_file = project_dir / "config.py"
    config_file.write_text(CONFIG_SOURCE)
    trial_file = project_dir / "trial.py"
    trial_file.write_text(TRIAL_SCRIPT)

    storage_url = str(tmp_path / "sweep.journal")
    tracking_dir = tmp_path / "tracking" / SWEEP
    tracking_dir.mkdir(parents=True)

    spec = SweepSubmission(
        trial_path=trial_file,
        config_path=config_file,
        study_name=SWEEP,
        storage_url=storage_url,
        n_trials=1,
        trial_relpath="trial.py",
        config_relpath="config.py",
        project_name=PROJECT,
        git_hash="deadbeef",
    )
    submit_result = SubmitResult(
        submissions=[
            JobSubmission(job_id="990001", role="trials", n_trials=1),
            JobSubmission(job_id="990002", role="post_hook"),
        ]
    )
    write_submission_events(
        build_submission_events(spec, "slurm", submit_result),
        LocalHost(),
        str(tracking_dir),
        "deploy.jsonl",
    )
    assert ship_events_file(
        tracking_dir / "submission" / "deploy.jsonl", base_url, API_KEY
    )

    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(
        RetryContext(
            study_name=SWEEP,
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            storage_path=storage_url,
            tracking_dir=str(tracking_dir),
            project_dir=str(project_dir),
            project_name=PROJECT,
            host_home=str(tmp_path),
        ).to_json()
    )

    optuna.create_study(
        study_name=SWEEP,
        storage=JournalStorage(JournalFileBackend(storage_url)),
        sampler=optuna.samplers.TPESampler(seed=7),
    )

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("JERNERICS_API_KEY", API_KEY)
        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name=SWEEP,
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name=PROJECT,
            server_addr=base_url,
            heartbeat_interval_s=0.05,
        )

    assert (
        run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tracking_dir),
            base_url=base_url,
            api_key=API_KEY,
        )
        == PipelineResult.SWEEP_COMPLETE
    )

    model_path = tracking_dir.parent / "artifacts-out" / "model-0.txt"
    yield SimpleNamespace(
        app=app,
        base_url=base_url,
        db_path=tmp_path / "server.sqlite",
        service=app.state.dashboard.service,
        model_bytes=model_path.read_bytes(),
    )


def _components(node: Component):
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, Component):
        yield from _components(children)
    elif isinstance(children, list | tuple):
        for child in children:
            if isinstance(child, Component):
                yield from _components(child)


def _page_text(page: Component) -> str:
    """Every string a rendered layout displays (labels, ids, facts)."""
    chunks: list[str] = []

    def collect(node: object) -> None:
        children = getattr(node, "children", None)
        if isinstance(children, str):
            chunks.append(children)
        elif isinstance(children, Component):
            collect(children)
        elif isinstance(children, list | tuple):
            for child in children:
                collect(child)

    collect(page)
    return " ".join(chunks)


def _grid_hrefs(grid: AgGrid) -> set[str]:
    """Navigation an AG Grid offers the user: markdown cells (the grid's
    markdown renderer emits anchors for them) and the artifact listing's
    row click, whose rowId is the artifact id and whose callback maps it
    through viewer_href."""
    markdown_fields = {
        column["field"]
        for column in getattr(grid, "columnDefs", None) or []
        if column.get("cellRenderer") == "markdown"
    }
    opens_viewer = getattr(grid, "getRowId", None) == _ARTIFACT_ROW_ID
    hrefs: set[str] = set()
    for row in getattr(grid, "rowData", None) or []:
        for field in markdown_fields:
            if isinstance(row.get(field), str):
                hrefs.update(_MARKDOWN_HREF.findall(row[field]))
        if opens_viewer and row.get("artifact_id"):
            hrefs.add(viewer_href(str(row["artifact_id"])))
    return hrefs


def _page_hrefs(page: Component) -> set[str]:
    """Every dashboard URL a rendered layout links or navigates to,
    resolved against ROUTES_BASE."""
    hrefs: set[str] = set()
    for component in _components(page):
        if isinstance(component, AgGrid):
            hrefs |= _grid_hrefs(component)
        elif isinstance(component, html.A) and component.href:
            hrefs.add(component.href)
    return {urljoin(LANDING, href) for href in hrefs}


def _walk_link_graph(service) -> dict[str, Component]:
    """BFS seeded with the landing URL only — every other visited URL
    came from a link harvested off an already-rendered layout."""
    pages: dict[str, Component] = {}
    queue = deque([LANDING])
    while queue:
        url = queue.popleft()
        if url in pages:
            continue
        page, _polls = page_content(url, service)
        pages[url] = page
        queue.extend(
            href
            for href in _page_hrefs(page) - pages.keys()
            if parse_route(href).kind != "not-found"
        )
    return pages


class TestSeededWorld:
    def test_pipeline_landed_one_finished_sweep_with_received_artifact(self, scenario):
        assert _rows(scenario.db_path, "SELECT project, name FROM sweeps") == [
            (PROJECT, SWEEP)
        ]
        assert _rows(scenario.db_path, "SELECT number, state FROM trials") == [
            (0, "completed")
        ]
        assert _rows(scenario.db_path, "SELECT outcome FROM executions") == [
            ("success",)
        ]
        received = _rows(
            scenario.db_path,
            "SELECT a.key, b.artifact_id IS NOT NULL FROM artifacts a "
            "LEFT JOIN artifact_blobs b USING (artifact_id) ORDER BY a.key",
        )
        assert ("model", 1) in received


class TestLinkGraphJourney:
    def test_landing_walk_stays_on_canonical_routes(self, scenario):
        pages = _walk_link_graph(scenario.service)
        kinds = {parse_route(url).kind for url in pages}
        assert {"project", "workspace"} <= kinds
        assert kinds <= {"project", "workspace", "artifact"}
        for page in pages.values():
            for href in _page_hrefs(page):
                assert parse_route(href).kind in {"project", "workspace", "artifact"}

    def test_focused_inspector_reaches_every_object_kind(self, scenario):
        sweep_id = _rows(scenario.db_path, "SELECT sweep_id FROM sweeps")[0][0]
        trial_id = _rows(scenario.db_path, "SELECT trial_id FROM trials")[0][0]
        execution_id = _rows(scenario.db_path, "SELECT execution_id FROM executions")[
            0
        ][0]
        artifact_id = _rows(
            scenario.db_path,
            "SELECT artifact_id FROM artifacts WHERE key = 'model'",
        )[0][0]

        for kind, object_id in (
            ("sweep", sweep_id),
            ("trial", trial_id),
            ("execution", execution_id),
        ):
            rendered = str(
                workspace.inspector_content(
                    scenario.service, {"kind": kind, "id": object_id}, 0
                )
            )
            assert short_id(object_id) in rendered

        viewer, _polls = page_content(viewer_href(artifact_id), scenario.service)
        assert short_id(artifact_id) in _page_text(viewer)
        back_links = [
            href
            for href in _page_hrefs(viewer)
            if href.startswith(f"{ROUTES_BASE}/project/")
        ]
        assert back_links
        for href in back_links:
            assert parse_route(href).kind == "workspace"

    def test_artifact_view_download_serves_the_seeded_bytes(self, scenario):
        artifact_id = _rows(
            scenario.db_path,
            "SELECT artifact_id FROM artifacts WHERE key = 'model'",
        )[0][0]
        page, _polls = page_content(viewer_href(artifact_id), scenario.service)
        downloads = [
            component.href
            for component in _components(page)
            if isinstance(component, html.A) and component.children == "Download"
        ]
        assert downloads == [raw_href(artifact_id)]

        denied = httpx.get(f"{scenario.base_url}{downloads[0]}", follow_redirects=False)
        assert denied.status_code == 303
        assert denied.headers["location"] == (
            f"{ROUTES_BASE}/login?next={quote(downloads[0], safe='')}"
        )

        served = httpx.get(
            f"{scenario.base_url}{downloads[0]}",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        assert served.status_code == 200
        assert served.content == scenario.model_bytes

    def test_every_grid_stays_text_selectable(self, scenario):
        """jernerics-eqn: AG Grid defaults to user-select: none; every
        grid the link graph reaches carries the copyability pair and
        keeps the options it already had."""
        pages = _walk_link_graph(scenario.service)
        grids = [
            (url, grid)
            for url, page in pages.items()
            for grid in _components(page)
            if isinstance(grid, AgGrid)
        ]
        assert {grid.id for _, grid in grids} >= {
            "sweep-grid",
            "analysis-family-grid",
            "analysis-trial-grid",
        }
        for url, grid in grids:
            options = grid.dashGridOptions or {}
            assert options.get("enableCellTextSelection") is True, (url, grid.id)
            assert options.get("ensureDomOrder") is True, (url, grid.id)
        by_id = {grid.id: grid for _, grid in grids}
        assert by_id["sweep-grid"].dashGridOptions["rowSelection"] == {
            "mode": "multiRow"
        }
        assert by_id["analysis-family-grid"].dashGridOptions["rowSelection"] == {
            "mode": "multiRow"
        }


class TestMountedDashboardHttp:
    def test_login_exchanges_key_for_session_and_index_renders(self, scenario):
        guarded = httpx.get(f"{scenario.base_url}/dashboard/", follow_redirects=False)
        assert guarded.status_code == 303
        assert guarded.headers["location"] == (
            f"{ROUTES_BASE}/login?next={quote(LANDING, safe='')}"
        )

        login = httpx.post(
            f"{scenario.base_url}/dashboard/login",
            data={"api_key": API_KEY},
            follow_redirects=False,
        )
        assert login.status_code == 303
        cookie = SimpleCookie()
        cookie.load(login.headers["set-cookie"])
        assert cookie[COOKIE_NAME].value

        index = httpx.get(
            f"{scenario.base_url}/dashboard/",
            headers={"Cookie": f"{COOKIE_NAME}={cookie[COOKIE_NAME].value}"},
        )
        assert index.status_code == 200
        assert "jernerics dashboard" in index.text

    def test_deep_link_login_round_trip_lands_on_target(self, scenario):
        deep = f"{ROUTES_BASE}/analysis?sel=tok%3D1"
        guarded = httpx.get(f"{scenario.base_url}{deep}", follow_redirects=False)
        assert guarded.status_code == 303
        assert guarded.headers["location"] == (
            f"{ROUTES_BASE}/login?next={quote(deep, safe='')}"
        )

        login = httpx.post(
            f"{scenario.base_url}{ROUTES_BASE}/login",
            data={"api_key": API_KEY, "next": deep},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert login.headers["location"] == deep

        cookie = SimpleCookie()
        cookie.load(login.headers["set-cookie"])
        landed = httpx.get(
            f"{scenario.base_url}{deep}",
            headers={"Cookie": f"{COOKIE_NAME}={cookie[COOKIE_NAME].value}"},
        )
        assert landed.status_code == 200
        assert "jernerics dashboard" in landed.text

    def test_mounted_callback_graph_keeps_url_search_shell_owned(self, scenario):
        """jernerics-8c9: the mounted app registers exactly one owner of
        ``url.search`` and it references only always-mounted shell ids,
        so no navigation can dispatch a callback into unmounted page
        components (the analysis-exit ReferenceError)."""
        dash_app = build_dash_app(scenario.app.state.dashboard)

        def output_specs(key: str) -> set[str]:
            stripped = key.removeprefix("..").removesuffix("..")
            return {part.split("@")[0] for part in stripped.split("...") if part}

        owners = [
            key for key in dash_app.callback_map if output_specs(key) == {"url.search"}
        ]
        assert owners == ["url.search"]
        owner = dash_app.callback_map["url.search"]
        shell_ids = {
            node.id
            for node in _components(shell())
            if isinstance(getattr(node, "id", None), str)
        }
        referenced = {dep["id"] for dep in owner["inputs"]}
        referenced |= {dep["id"] for dep in owner.get("state", [])}
        assert referenced <= shell_ids


class TestArtifactRowClickNavigation:
    """The artifact listing's row-click navigation, over the mounted
    server. In the browser, dash-ag-grid evaluates getRowId only when it
    is the registered-function form (an inline JS string is inert
    without dangerously_allow_code) and fires cellClicked with the
    evaluated row id; _open_artifact must then map only real UUIDs to
    the viewer URL."""

    @staticmethod
    def _cell_clicked(base_url: str, row_id: str | None) -> httpx.Response:
        """POST the _dash-update-component payload a real cellClicked
        event produces."""
        return httpx.post(
            f"{base_url}{ROUTES_BASE}/_dash-update-component",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "output": "url.pathname",
                "outputs": {"id": "url", "property": "pathname"},
                "inputs": [
                    {
                        "id": "artifact-grid",
                        "property": "cellClicked",
                        "value": {"rowId": row_id},
                    }
                ],
                "changedPropIds": ["artifact-grid.cellClicked"],
            },
        )

    def test_row_id_expression_is_a_registered_asset_function(self, scenario):
        trial_id = _rows(scenario.db_path, "SELECT trial_id FROM trials")[0][0]
        page = workspace.inspector_content(
            scenario.service, {"kind": "trial", "id": trial_id}, 0
        )
        grid = next(
            component
            for component in _components(page)
            if isinstance(component, AgGrid) and component.id == "artifact-grid"
        )
        assert grid.getRowId == _ARTIFACT_ROW_ID

        auth = {"Authorization": f"Bearer {API_KEY}"}
        asset = httpx.get(
            f"{scenario.base_url}{ROUTES_BASE}/assets/dashAgGridFunctions.js",
            headers=auth,
        )
        assert asset.status_code == 200
        assert "jernericsArtifactRowId" in asset.text
        index = httpx.get(f"{scenario.base_url}{ROUTES_BASE}/", headers=auth)
        assert "assets/dashAgGridFunctions.js" in index.text

    def test_cell_clicked_with_artifact_uuid_navigates_to_viewer(self, scenario):
        artifact_id = _rows(
            scenario.db_path, "SELECT artifact_id FROM artifacts WHERE key = 'model'"
        )[0][0]
        response = self._cell_clicked(scenario.base_url, artifact_id)
        assert response.status_code == 200
        assert response.json()["response"]["url"]["pathname"] == viewer_href(
            artifact_id
        )

        shuffled = self._cell_clicked(scenario.base_url, artifact_id.upper())
        assert shuffled.json()["response"]["url"]["pathname"] == viewer_href(
            artifact_id
        )

    def test_cell_clicked_with_non_uuid_row_id_never_navigates(self, scenario):
        for row_id in ("undefined", "", "../admin", None):
            response = self._cell_clicked(scenario.base_url, row_id)
            assert response.status_code == 204, row_id
