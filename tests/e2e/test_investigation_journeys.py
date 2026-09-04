import contextlib
import json
import socket
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

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
from jernerics.tracking.client import TrackingClient
from jernerics_schema import (
    JERNERICS_NAMESPACE,
    Selection,
    SweepSnapshotEvent,
    decode_selection,
    sweep_id_for,
)
from jernerics_server.dashboard import workspace
from jernerics_server.dashboard.analysis import (
    investigation_scope_state,
)
from jernerics_server.dashboard.app import build_dash_app
from jernerics_server.dashboard.callbacks import page_content
from jernerics_server.dashboard.routes import ROUTES_BASE
from jernerics_server.http import create_app
from jernerics_server.store import Store
from optuna.storages.journal import JournalFileBackend, JournalStorage

optuna.logging.set_verbosity(optuna.logging.WARNING)

API_KEY = "inv-journeys-secret"
PROJECT = "atlas"

ROBERTS = "roberts-lr"
ROBERTS_SGD = "roberts-lr-sgd"
ROBERTS_PARTIAL = "roberts-lr-partial"
ROBERTS_FLAGGED = "roberts-lr-flagged"
HOB = "hobbes-width"

MAIN_INV = "lr-comparison"

LR_FIXED_PARAMS = (
    {"lr": 0.1, "seed": 1, "act": "relu"},
    {"lr": 0.3, "seed": 2, "act": "gelu"},
)
WIDTH_FIXED_PARAMS = ({"width": 16, "drop": 0.1},)

SIG_0 = "act=relu · lr=0.1 · seed=1"
SIG_1 = "act=gelu · lr=0.3 · seed=2"

SEEDS = (
    {
        "name": ROBERTS,
        "config": "lr",
        "n_trials": 2,
        "runs": 2,
        "optimizer": "adam",
        "loss_base": 0.5,
        "git": "deadbeef",
        "acc": True,
        "complete": True,
    },
    {
        "name": ROBERTS_SGD,
        "config": "lr",
        "n_trials": 2,
        "runs": 2,
        "optimizer": "sgd",
        "loss_base": 0.8,
        "git": "deadbeef",
        "acc": True,
        "complete": True,
    },
    {
        "name": ROBERTS_PARTIAL,
        "config": "lr",
        "n_trials": 2,
        "runs": 1,
        "optimizer": "adam",
        "loss_base": 0.5,
        "git": "deadbeef",
        "acc": False,
        "complete": False,
    },
    {
        "name": ROBERTS_FLAGGED,
        "config": "lr",
        "n_trials": 2,
        "runs": 2,
        "optimizer": "adadelta",
        "loss_base": 1.2,
        "git": "cafed00d",
        "acc": True,
        "complete": True,
    },
    {
        "name": HOB,
        "config": "width",
        "n_trials": 1,
        "runs": 1,
        "optimizer": "adam",
        "loss_base": 0.4,
        "git": "deadbeef",
        "acc": False,
        "complete": True,
    },
)

ACC_BLOCK = (
    'for step in range(2):\n    tracker.log_value("acc", 0.80 + 0.01 * step, step=step)'
)

TRIAL_SCRIPT = """\
import os
from jernerics import trial_config, trial_tracker

config = trial_config()
tracker = trial_tracker()
number = int(os.environ["JERNERICS_TRIAL_NUMBER"])

tracker.log_param("optimizer", "{optimizer}")
tracker.log_param("batch_size", 32)
for step in range(3):
    tracker.log_value("loss", {loss_base} + 0.05 * step + 0.1 * number, step=step)
{acc_block}
tracker.finish({{"loss": {final}}})
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

CONFIG_LR = """\
base = {"note": "inv-journeys"}
n_trials = 2

def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 0.05, 0.5),
        "seed": trial.suggest_int("seed", 1, 5),
        "act": trial.suggest_categorical("act", ["relu", "gelu"]),
    }

def objective(results):
    return results["loss"]
"""

CONFIG_WIDTH = """\
base = {"note": "inv-journeys"}
n_trials = 1

def search_space(trial):
    return {
        "width": trial.suggest_int("width", 8, 64),
        "drop": trial.suggest_float("drop", 0.0, 0.5),
    }

def objective(results):
    return results["loss"]
"""


def _final_loss(loss_base: float, number: int) -> float:
    return loss_base + 0.05 * 2 + 0.1 * number


def _trial_source(seed: dict) -> str:
    return TRIAL_SCRIPT.format(
        optimizer=seed["optimizer"],
        loss_base=seed["loss_base"],
        acc_block=ACC_BLOCK if seed["acc"] else "",
        final=f"{seed['loss_base']} + 0.05 * 2 + 0.1 * number",
    )


def _start_server(tmp_path: Path, port: int | None = None) -> tuple[object, str]:
    if port is None:
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


def _seeded_server(tmp_path: Path, port: int | None = None) -> SimpleNamespace:
    """Five real sweeps driven through the pipeline into one fresh
    authenticated server: four share the lr search space and sampled
    signatures (one incomplete, one later flagged invalid), one runs a
    disjoint width space; manual params and step values are rich enough
    for previews, signature matching, and parcoords."""
    app, base_url = _start_server(tmp_path, port=port)

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(PYPROJECT_SOURCE)
    (project_dir / "config_lr.py").write_text(CONFIG_LR)
    (project_dir / "config_width.py").write_text(CONFIG_WIDTH)

    sweep_ids = {
        seed["name"]: str(sweep_id_for(PROJECT, seed["name"])) for seed in SEEDS
    }
    for seed in SEEDS:
        name = seed["name"]
        config_name = f"config_{seed['config']}.py"
        trial_file = project_dir / f"trial_{name}.py"
        trial_file.write_text(_trial_source(seed))
        tracking_dir = tmp_path / "tracking" / name
        tracking_dir.mkdir(parents=True)
        storage_url = str(tmp_path / f"{name}.journal")

        spec = SweepSubmission(
            trial_path=trial_file,
            config_path=project_dir / config_name,
            study_name=name,
            storage_url=storage_url,
            n_trials=seed["n_trials"],
            trial_relpath=trial_file.name,
            config_relpath=config_name,
            project_name=PROJECT,
            git_hash=seed["git"],
        )
        submit_result = SubmitResult(
            submissions=[
                JobSubmission(
                    job_id="990001", role="trials", n_trials=seed["n_trials"]
                ),
                JobSubmission(job_id="990002", role="post_hook"),
            ]
        )
        events = build_submission_events(spec, "slurm", submit_result)
        write_submission_events(events, LocalHost(), str(tracking_dir), "deploy.jsonl")
        assert ship_events_file(
            tracking_dir / "submission" / "deploy.jsonl", base_url, API_KEY
        )

        study = optuna.create_study(
            study_name=name,
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        fixed = LR_FIXED_PARAMS if seed["config"] == "lr" else WIDTH_FIXED_PARAMS
        for params in fixed[: seed["n_trials"]]:
            study.enqueue_trial(params)

        ctx_path = tmp_path / f"{name}-ctx.json"
        ctx_path.write_text(
            RetryContext(
                study_name=name,
                backend_name="slurm",
                trial_relpath=trial_file.name,
                config_relpath=config_name,
                storage_path=storage_url,
                tracking_dir=str(tracking_dir),
                project_dir=str(project_dir),
                project_name=PROJECT,
                host_home=str(tmp_path),
            ).to_json()
        )

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setenv("JERNERICS_API_KEY", API_KEY)
            for _ in range(seed["runs"]):
                run_trial(
                    trial_file=str(trial_file),
                    config_file=str(project_dir / config_name),
                    study_name=name,
                    storage_url=storage_url,
                    tracking_dir=str(tracking_dir),
                    project_name=PROJECT,
                    server_addr=base_url,
                    heartbeat_interval_s=0.05,
                )

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tracking_dir),
            base_url=base_url,
            api_key=API_KEY,
        )
        if seed["complete"]:
            assert result == PipelineResult.SWEEP_COMPLETE, name
            # The terminal snapshot ships after the post-hook: its
            # reconcile sweep snapshot (running) would overwrite an
            # earlier terminal state.
            write_submission_events(
                [
                    SweepSnapshotEvent(
                        event_id=uuid.uuid4(),
                        recorded_at=datetime.now(UTC),
                        project=PROJECT,
                        sweep_id=sweep_id_for(PROJECT, name),
                        name=name,
                        state="completed",
                    )
                ],
                LocalHost(),
                str(tracking_dir),
                "terminal.jsonl",
            )
            assert ship_events_file(
                tracking_dir / "submission" / "terminal.jsonl",
                base_url,
                API_KEY,
            )

    service = app.state.dashboard.service
    service.mark_sweep_invalid(sweep_ids[ROBERTS_FLAGGED], "seeded invalid")
    return SimpleNamespace(
        app=app,
        base_url=base_url,
        db_path=tmp_path / "server.sqlite",
        service=service,
        sweep_ids=sweep_ids,
        tmp_path=tmp_path,
    )


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    return _seeded_server(tmp_path_factory.mktemp("investigation-journeys"))


def _rows(db_path: Path, sql: str, params: list | None = None) -> list[tuple]:
    with Store(db_path) as store:
        return store.query(sql, params)[1]


def _components(node: Component):
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, Component):
        yield from _components(children)
    elif isinstance(children, list | tuple):
        for child in children:
            yield from _components(child)


def _page_text(page: Component) -> str:
    chunks: list[str] = []

    def collect(node: object) -> None:
        children = getattr(node, "children", None)
        if isinstance(children, str | int | float):
            chunks.append(str(children))
        elif isinstance(children, Component):
            collect(children)
        elif isinstance(children, list | tuple):
            for child in children:
                if isinstance(child, str | int | float):
                    chunks.append(str(child))
                else:
                    collect(child)

    collect(page)
    return " ".join(chunks)


def _by_id(page: Component, component_id) -> Component:
    return next(
        node for node in _components(page) if getattr(node, "id", None) == component_id
    )


def _pattern(page: Component, key: str) -> list:
    return [
        node
        for node in _components(page)
        if isinstance(getattr(node, "id", None), dict) and key in node.id
    ]


def _grid_rows(page: Component, grid_id: str) -> list[dict]:
    grid = _by_id(page, grid_id)
    assert isinstance(grid, AgGrid), grid_id
    return grid.rowData or []


def _dispatch(world, needle: set[str], input_id, inputs, state=(), changed=()):
    """Fire one registered callback through Dash's dispatch endpoint,
    exactly as the browser would. ``needle`` narrows the output key,
    ``input_id`` pins the callback by one of its inputs."""
    callback_map = build_dash_app(world.app.state.dashboard).callback_map
    # Pattern ids appear in the map as compact JSON with ["ALL"] values.
    wanted = (
        json.dumps({name: ["ALL"] for name in input_id}, separators=(",", ":"))
        if isinstance(input_id, dict)
        else input_id
    )
    key = next(
        key
        for key, spec in callback_map.items()
        if all(token in key for token in needle)
        and any(dep.get("id") == wanted for dep in spec.get("inputs", []))
    )
    outputs = []
    for spec in key.removeprefix("..").removesuffix("..").split("..."):
        if not spec:
            continue
        prop = spec.rsplit(".", 1)[1]
        raw = spec.rsplit(".", 1)[0]
        ident = json.loads(raw) if raw.startswith("{") else raw
        if isinstance(ident, dict):
            ident = {
                name: "canvas" if values == ["ALL"] else values
                for name, values in ident.items()
            }
        outputs.append({"id": ident, "property": prop})
    response = httpx.post(
        f"{world.base_url}{ROUTES_BASE}/_dash-update-component",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "output": key,
            "outputs": outputs[0] if len(outputs) == 1 else outputs,
            "inputs": inputs,
            "state": list(state),
            "changedPropIds": list(changed),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["response"]


def _inv_url(investigation_id: str) -> str:
    return f"{ROUTES_BASE}/project/{PROJECT}/investigation/{investigation_id}"


def _view_url(investigation_id: str, view: str, member: str | None) -> tuple[str, str]:
    """(pathname, search) for one investigation view."""
    search = workspace.investigation_search(view, member)
    return _inv_url(investigation_id), search


def _selection_tokens(page: Component) -> list[Selection]:
    tokens = []
    for node in _components(page):
        if isinstance(node, html.Pre) and node.className == "config-json":
            with contextlib.suppress(Exception):
                tokens.append(decode_selection(str(node.children)))
    return tokens


class TestSeededWorld:
    def test_five_sweeps_seeded_with_expected_trial_facts(self, world):
        names = dict(_rows(world.db_path, "SELECT name, sweep_id FROM sweeps"))
        assert set(names) == set(world.sweep_ids)
        trials = _rows(
            world.db_path,
            "SELECT s.name, t.state, COUNT(*) FROM trials t "
            "JOIN sweeps s USING (sweep_id) GROUP BY s.name, t.state "
            "ORDER BY s.name, t.state",
        )
        assert trials == [
            (HOB, "completed", 1),
            (ROBERTS, "completed", 2),
            (ROBERTS_FLAGGED, "completed", 2),
            (ROBERTS_PARTIAL, "completed", 1),
            (ROBERTS_SGD, "completed", 2),
        ]
        expected = dict(
            _rows(
                world.db_path,
                "SELECT s.name, MAX(sub.expected_trials) FROM submissions sub "
                "JOIN sweeps s USING (sweep_id) GROUP BY s.name",
            )
        )
        assert expected[ROBERTS_PARTIAL] == 2
        assert expected[HOB] == 1
        states = dict(_rows(world.db_path, "SELECT name, state FROM sweeps"))
        assert states[ROBERTS_PARTIAL] != "completed"
        assert {name for name, state in states.items() if state == "completed"} == {
            ROBERTS,
            ROBERTS_SGD,
            ROBERTS_FLAGGED,
            HOB,
        }
        optimizer = dict(
            _rows(
                world.db_path,
                "SELECT s.name, group_concat(DISTINCT p.value_json) "
                "FROM trial_params p JOIN trials t USING (trial_id) "
                "JOIN sweeps s USING (sweep_id) "
                "WHERE p.kind = 'manual' AND p.key = 'optimizer' "
                "GROUP BY s.name",
            )
        )
        assert optimizer == {
            ROBERTS: '"adam"',
            ROBERTS_SGD: '"sgd"',
            ROBERTS_PARTIAL: '"adam"',
            ROBERTS_FLAGGED: '"adadelta"',
            HOB: '"adam"',
        }


def _picked(world) -> list[str]:
    return sorted(
        world.sweep_ids[name]
        for name in (ROBERTS, ROBERTS_SGD, ROBERTS_PARTIAL, ROBERTS_FLAGGED)
    )


@pytest.fixture(scope="module")
def saved(world):
    """The editor's Create investigation action, fired through the
    dispatch endpoint over the mounted app: the URL flips to the
    investigation workspace."""
    state = {
        "picked": _picked(world),
        "saved": [],
        "name": MAIN_INV,
        "factor": "optimizer",
        "outcome": "loss",
    }
    return _dispatch(
        world,
        needle={"inv-edit-message", "url.search"},
        input_id={"inv-edit-save": "save"},
        inputs=[
            {
                "id": {"inv-edit-save": "save"},
                "property": "n_clicks",
                "value": 1,
            }
        ],
        state=[
            # A wildcard State arrives grouped: the browser sends one
            # list of matched items per dependency spec.
            [
                {
                    "id": {"inv-edit-state": "members"},
                    "property": "data",
                    "value": state,
                }
            ],
            {
                "id": "url",
                "property": "pathname",
                "value": f"{_inv_url('new')}",
            },
        ],
        changed=['{"inv-edit-save":"save"}.n_clicks'],
    )


class TestDashboardJourney:
    """Create from selected sweeps, preview, save, then every workspace
    view over the saved investigation — rendered pages and the dispatch
    endpoint, not internals."""

    def test_editor_new_page_previews_the_seeded_membership(self, world):
        search = "?sweeps=" + ",".join(_picked(world))
        page, _polls = page_content(_inv_url("new"), world.service, search=search)
        text = _page_text(page)
        assert "New Investigation" in text
        assert "project members picked" in text
        assert "+4 -0 (unsaved)" in text
        factor_lines = [
            "param optimizer — 4 of 4 members",
            "config source config_source — 4 of 4 members",
            "name token flagged — 1 of 4 members",
            "name token lr — 4 of 4 members",
            "name token partial — 1 of 4 members",
            "name token roberts — 4 of 4 members",
            "name token sgd — 1 of 4 members",
        ]
        outcome_lines = ["loss — 4 of 4 members", "acc — 3 of 4 members"]
        for line in factor_lines + outcome_lines:
            assert line in text, line
        positions = [text.index(line) for line in factor_lines + outcome_lines]
        assert positions == sorted(positions)
        assert (
            "git hash divergence: differing git_hash across members: "
            "cafed00d, deadbeef" in text
        )
        factor_dropdown = _by_id(page, {"inv-edit-factor": "factor"})
        assert [option["value"] for option in factor_dropdown.options] == [
            "optimizer",
            "config_source",
            "flagged",
            "lr",
            "partial",
            "roberts",
            "sgd",
        ]
        outcome_dropdown = _by_id(page, {"inv-edit-outcome": "outcome"})
        assert [option["value"] for option in outcome_dropdown.options] == [
            "loss",
            "acc",
        ]
        picked = set(_picked(world))
        picks = _pattern(page, "inv-edit-pick")
        assert len(picks) == 5
        assert all(
            node.value == [node.id["inv-edit-pick"]]
            if node.id["inv-edit-pick"] in picked
            else node.value == []
            for node in picks
        )

    def test_save_creates_the_investigation_and_opens_the_workspace(self, world, saved):
        pathname = saved["url"]["pathname"]
        investigation_id = pathname.rsplit("/", 1)[1]
        assert saved["url"]["search"] == ""
        detail = world.service.investigation_detail(investigation_id)
        assert detail.investigation.name == MAIN_INV
        assert detail.investigation.factor == "optimizer"
        assert detail.investigation.outcome == "loss"
        assert {str(m) for m in detail.investigation.members} == set(_picked(world))
        page, _polls = page_content(pathname, world.service)
        text = " ".join(_page_text(page).split())
        assert MAIN_INV in text
        assert (
            "factor optimizer · outcome loss (final) · matching by "
            "exact sampled signature" in text
        )
        for label in ("Compare", "Series", "Points", "Search"):
            assert label in text
        assert "Members 4" in text
        assert "Valid 3" in text
        assert "Marked invalid (excluded by default) 1" in text
        assert "With outcome 4" in text
        assert "Incomplete 1" in text
        toggle = _by_id(page, "inv-include-invalid")
        assert toggle.value == []

    def test_compare_renders_signature_matching_and_coverage(self, world, saved):
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        page, _polls = page_content(_inv_url(investigation_id), world.service)
        text = _page_text(page)
        assert "Outcome heatmap" in text
        assert "Median over common signatures" in text
        assert (
            "factor by exact sampled signature (act, lr, seed) — "
            "no imputation, no outliers suppressed." in text
        )
        doc = world.service.investigation_compare(investigation_id)
        assert doc.signature_keys == ("act", "lr", "seed")
        # the matched table renders one column per analyzable member;
        # the common signature carries its chip, values render as text
        assert SIG_0 in text and SIG_1 in text
        assert "Matched comparison (loss)" in text
        assert (
            "2 signatures matched by ≥2 analyzable members · 1 common to "
            "all 3 · medians pool matched trials; no imputation, no "
            "outliers suppressed." in text
        )
        # The factor column merges every carry source: the manual param
        # plus the submission config source.
        assert "adam / config_lr.py" in text
        assert "config_lr.py / sgd" in text
        assert "adadelta / config_lr.py" in text
        # usable fractions and curation badges ride their row cells
        assert "roberts-lr completed 2/2 2/2" in text
        assert "roberts-lr-partial incomplete 1/2 1/2" in text
        assert "roberts-lr-flagged invalid archived completed 2/2 2/2" in text

        # every member name links to the sweep page carrying the
        # investigation return path
        for name in (ROBERTS, ROBERTS_SGD, ROBERTS_PARTIAL, ROBERTS_FLAGGED):
            expected = (
                f"{ROUTES_BASE}/project/{PROJECT}/sweep/{world.sweep_ids[name]}"
                f"?via={investigation_id}"
            )
            assert any(
                isinstance(node, html.A)
                and node.href == expected
                and node.children == name
                for node in _components(page)
            ), name

    def test_series_and_points_scope_to_the_materialized_member(self, world, saved):
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        partial = world.sweep_ids[ROBERTS_PARTIAL]
        path, search = _view_url(investigation_id, "series", partial)
        series_page, _polls = page_content(path, world.service, search=search)
        text = _page_text(series_page)
        assert f"Scoped to member {ROBERTS_PARTIAL}" in text
        clear_button = _by_id(series_page, "inv-member-clear")
        assert clear_button.style == {}
        key_dropdown = _by_id(series_page, "analysis-key")
        assert [option["value"] for option in key_dropdown.options] == ["loss"]
        path, search = _view_url(investigation_id, "points", partial)
        points_page, _polls = page_content(path, world.service, search=search)
        scoped_rows = _grid_rows(points_page, "inv-points-grid")
        assert {row["sweep"] for row in scoped_rows} == {ROBERTS_PARTIAL}
        assert len(scoped_rows) == 1

        path, search = _view_url(investigation_id, "points", None)
        all_points, _polls = page_content(path, world.service, search=search)
        rows = _grid_rows(all_points, "inv-points-grid")
        assert {row["sweep"] for row in rows} == {
            ROBERTS,
            ROBERTS_SGD,
            ROBERTS_PARTIAL,
            ROBERTS_FLAGGED,
        }
        assert len(rows) == 7
        path, search = _view_url(investigation_id, "series", None)
        all_series, _polls = page_content(path, world.service, search=search)
        key_dropdown = _by_id(all_series, "analysis-key")
        assert [option["value"] for option in key_dropdown.options] == [
            "acc",
            "loss",
        ]

    def test_sweep_opened_from_the_investigation_returns_via_the_hub(
        self, world, saved
    ):
        """The member sweep link carries the investigation return path
        (?via=); the sweep hub page consuming it lands with R4
        (jernerics-zj9b) — this journey pins the link contract."""
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        sgd = world.sweep_ids[ROBERTS_SGD]
        path, search = _view_url(investigation_id, "compare", None)
        page, _polls = page_content(path, world.service, search=search)
        via_link = next(
            node
            for node in _components(page)
            if isinstance(node, html.A)
            and getattr(node, "href", None)
            and node.href.endswith(f"/sweep/{sgd}?via={investigation_id}")
        )
        assert via_link.children == ROBERTS_SGD

    def test_open_in_python_token_decodes_to_the_materialized_selection(
        self, world, saved
    ):
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        record = world.service.investigation_detail(investigation_id)
        path, search = _view_url(investigation_id, "python", None)
        page, _polls = page_content(path, world.service, search=search)
        tokens = _selection_tokens(page)
        assert Selection(project=PROJECT, sweeps=record.investigation.members) in (
            tokens
        )

        sgd = world.sweep_ids[ROBERTS_SGD]
        scoped_path, scoped_search = _view_url(investigation_id, "python", sgd)
        scoped_page, _polls = page_content(
            scoped_path, world.service, search=scoped_search
        )
        assert Selection(
            project=PROJECT, sweeps=(uuid.UUID(sgd),)
        ) in _selection_tokens(scoped_page)


class TestAgentClientJourney:
    """The whole agent surface through the typed client only: no
    dashboard, no HTTP scraping — list, preview, idempotent writes,
    materialization, archive/restore, and the JSON shapes."""

    def test_full_journey_over_the_typed_client(self, world):
        handles = world.sweep_ids
        picked = sorted(
            handles[name]
            for name in (ROBERTS, ROBERTS_SGD, ROBERTS_PARTIAL, ROBERTS_FLAGGED)
        )
        with TrackingClient(world.base_url, api_key=API_KEY) as client:
            handle = client.project(PROJECT)
            assert MAIN_INV in [record.name for record in handle.investigations()]

            preview = handle.investigation_preview(picked)
            assert set(preview.model_dump()) == {
                "project",
                "member_count",
                "factors",
                "outcomes",
                "warnings",
            }
            assert preview.project == PROJECT
            assert preview.member_count == 4
            assert [factor.model_dump() for factor in preview.factors] == [
                {"kind": "manual_param", "name": "optimizer", "members": 4},
                {"kind": "config_source", "name": "config_source", "members": 4},
                {"kind": "name_token", "name": "flagged", "members": 1},
                {"kind": "name_token", "name": "lr", "members": 4},
                {"kind": "name_token", "name": "partial", "members": 1},
                {"kind": "name_token", "name": "roberts", "members": 4},
                {"kind": "name_token", "name": "sgd", "members": 1},
            ]
            assert [outcome.model_dump() for outcome in preview.outcomes] == [
                {"key": "loss", "members": 4},
                {"key": "acc", "members": 3},
            ]
            assert [warning.kind for warning in preview.warnings] == [
                "git_hash_divergence"
            ]
            assert preview == handle.investigation_preview(picked)

            expected_id = str(
                uuid.uuid5(
                    JERNERICS_NAMESPACE,
                    f"investigation:{PROJECT}:agent-curated",
                )
            )
            created = handle.create_investigation(
                "agent-curated", "optimizer", "loss", [handles[ROBERTS]]
            )
            retry = handle.create_investigation(
                "agent-curated", "optimizer", "loss", [handles[ROBERTS]]
            )
            assert created == retry
            assert str(created.id) == expected_id
            assert created.project == PROJECT
            assert created.members == (uuid.UUID(handles[ROBERTS]),)
            assert set(created.model_dump()) == {
                "id",
                "project",
                "name",
                "factor",
                "outcome",
                "replicate_factor",
                "archived_ns",
                "created_ns",
                "updated_ns",
                "members",
            }

            investigation_id = str(created.id)
            added = handle.add_investigation_members(
                investigation_id, [handles[ROBERTS_SGD]]
            )
            retried = handle.add_investigation_members(
                investigation_id, [handles[ROBERTS_SGD]]
            )
            assert added == retried
            assert added.members == (
                uuid.UUID(handles[ROBERTS]),
                uuid.UUID(handles[ROBERTS_SGD]),
            )
            removed = handle.remove_investigation_members(
                investigation_id, [handles[ROBERTS_SGD]]
            )
            assert removed == handle.remove_investigation_members(
                investigation_id, [handles[ROBERTS_SGD]]
            )
            assert removed.members == (uuid.UUID(handles[ROBERTS]),)

            selection = handle.investigation_selection(investigation_id)
            assert selection == Selection(
                project=PROJECT, sweeps=(uuid.UUID(handles[ROBERTS]),)
            )

            detail = handle.investigation(investigation_id)
            assert set(detail.model_dump()) == {"investigation", "coverage"}
            assert set(detail.coverage.model_dump()) == {
                "members",
                "with_outcome",
                "completed",
                "invalid",
                "last_activity_ns",
            }
            assert detail.coverage.members == 1
            assert detail.coverage.with_outcome == 1
            assert detail.coverage.completed == 1
            assert detail.coverage.invalid == 0

            archived = handle.archive_investigation(investigation_id)
            assert archived == handle.archive_investigation(investigation_id)
            assert archived.archived_ns is not None
            assert investigation_id not in {
                str(record.id) for record in handle.investigations()
            }
            assert investigation_id in {
                str(record.id)
                for record in handle.investigations(include_archived=True)
            }

            restored = handle.restore_investigation(investigation_id)
            assert restored == handle.restore_investigation(investigation_id)
            assert restored.archived_ns is None
            assert investigation_id in {
                str(record.id) for record in handle.investigations()
            }


class TestEdgeFacts:
    """Analysis edges cross-checked against the seeded facts: invalid
    exclusion, archived inclusion, live incomplete coverage, unknown
    member scope, and the no-overlap honesty."""

    @pytest.fixture(scope="class")
    def flagged_only(self, world):
        record = world.service.create_investigation(
            PROJECT,
            "atlas-flagged-only",
            "optimizer",
            "loss",
            members=[world.sweep_ids[ROBERTS_FLAGGED]],
        )
        return str(record.id)

    @pytest.fixture(scope="class")
    def archived_pair(self, world):
        world.service.archive_sweep(world.sweep_ids[HOB])
        record = world.service.create_investigation(
            PROJECT,
            "atlas-archived-pair",
            "optimizer",
            "loss",
            members=[world.sweep_ids[ROBERTS_SGD], world.sweep_ids[HOB]],
        )
        return str(record.id)

    @pytest.fixture(scope="class")
    def width_mix(self, world):
        record = world.service.create_investigation(
            PROJECT,
            "atlas-width-mix",
            "optimizer",
            "loss",
            members=[world.sweep_ids[ROBERTS], world.sweep_ids[HOB]],
        )
        return str(record.id)

    def test_invalid_members_excluded_from_the_analysis_set_by_default(
        self, world, saved
    ):
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        doc = world.service.investigation_compare(investigation_id)
        flagged = world.sweep_ids[ROBERTS_FLAGGED]
        assert flagged not in doc.analyzable
        assert doc.excluded_data_bearing == 1
        member = next(m for m in doc.members if m.sweep_id == flagged)
        assert member.invalid and member.usable > 0

    def test_include_invalid_rejoins_the_analysis_set(self, world, saved):
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        flagged = world.sweep_ids[ROBERTS_FLAGGED]
        a = world.sweep_ids[ROBERTS]
        d = world.sweep_ids[ROBERTS_PARTIAL]
        b = world.sweep_ids[ROBERTS_SGD]
        doc = world.service.investigation_compare(
            investigation_id, include_invalid=True
        )
        assert set(doc.analyzable) == {a, b, d, flagged}
        assert [row.matched for row in doc.signatures] == [4, 3]
        assert doc.signatures[0].common
        assert not doc.signatures[1].common
        assert doc.signatures[0].values[flagged] == _final_loss(1.2, 0)
        body = workspace.compare_body(doc, PROJECT, "loss", investigation_id, True)
        text = _page_text(html.Div(body))
        assert (
            "2 signatures matched by ≥2 analyzable members · 1 common to "
            "all 4 · medians pool matched trials; no imputation, no "
            "outliers suppressed." in text
        )

    def test_invalid_only_members_render_the_honest_empty_state(
        self, world, flagged_only
    ):
        page, _polls = page_content(_inv_url(flagged_only), world.service)
        text = _page_text(page)
        assert "Marked invalid (excluded by default) 1" in text
        assert (
            "No analyzable members in the analysis set — 1 data-bearing "
            "members are marked invalid (excluded by default) and 0 have "
            "no outcome data." in text
        )
        assert "Tick “include invalid members in analysis”" in text
        assert "Outcome heatmap" not in text
        assert "Median over common signatures" not in text

    def test_archived_member_stays_in_the_analysis_set(self, world, archived_pair):
        doc = world.service.investigation_compare(archived_pair)
        hob = world.sweep_ids[HOB]
        member = next(m for m in doc.members if m.sweep_id == hob)
        assert member.archived and not member.invalid
        assert member.usable == 1
        assert hob in doc.analyzable
        page, _polls = page_content(_inv_url(archived_pair), world.service)
        assert f"{HOB} archived" in _page_text(page)

    def test_incomplete_member_reflects_live_coverage(self, world, saved):
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        detail = world.service.investigation_detail(investigation_id)
        assert detail.coverage.members == 4
        assert detail.coverage.completed == 3
        assert detail.coverage.with_outcome == 4
        assert detail.coverage.invalid == 1
        doc = world.service.investigation_compare(investigation_id)
        partial = next(
            m for m in doc.members if m.sweep_id == world.sweep_ids[ROBERTS_PARTIAL]
        )
        assert partial.completed == 1
        assert partial.usable == 1
        assert partial.state != "completed"

    def test_unknown_member_scope_falls_back_to_all_members(self, world, saved):
        investigation_id = saved["url"]["pathname"].rsplit("/", 1)[1]
        record = world.service.investigation_detail(investigation_id)
        tray, scoped = investigation_scope_state(
            record.investigation.members, str(uuid.uuid4())
        )
        assert scoped is None
        assert set(tray["sweeps"]) == {
            str(member) for member in record.investigation.members
        }
        path, search = _view_url(investigation_id, "series", str(uuid.uuid4()))
        page, _polls = page_content(path, world.service, search=search)
        text = _page_text(page)
        assert "Scoped to member" not in text
        clear_button = _by_id(page, "inv-member-clear")
        assert clear_button.style == {"display": "none"}
        key_dropdown = _by_id(page, "analysis-key")
        assert [option["value"] for option in key_dropdown.options] == [
            "acc",
            "loss",
        ]

    def test_no_global_overlap_renders_the_honest_state(self, world, width_mix):
        doc = world.service.investigation_compare(width_mix)
        assert set(doc.analyzable) == {
            world.sweep_ids[ROBERTS],
            world.sweep_ids[HOB],
        }
        a = world.sweep_ids[ROBERTS]
        hob = world.sweep_ids[HOB]
        # Signatures exist per member but no signature reaches two
        # analyzable members: nothing is common, nothing ranks.
        assert {row.label for row in doc.signatures} == {
            SIG_0,
            SIG_1,
            "drop=0.1 · width=16",
        }
        assert all(row.matched == 1 and not row.common for row in doc.signatures)
        by_label = {row.label: row for row in doc.signatures}
        assert by_label[SIG_0].values == {a: _final_loss(0.5, 0), hob: None}
        assert by_label["drop=0.1 · width=16"].values == {
            a: None,
            hob: _final_loss(0.4, 0),
        }
        page, _polls = page_content(_inv_url(width_mix), world.service)
        text = _page_text(page)
        assert (
            "No sampled signature is completed by all 2 analyzable "
            "members — no global overlap. Pairwise matches are listed "
            "below; no ranking is manufactured." in text
        )
        assert "Outcome heatmap" not in text
        assert "Median over common signatures" not in text
        assert "Matched comparison" not in text
        # the member inventory still renders
        assert ROBERTS in text and HOB in text


class TestIndexAndArchive:
    @pytest.fixture(scope="class")
    def pending_archive(self, world):
        record = world.service.create_investigation(
            PROJECT,
            "atlas-pending-archive",
            "optimizer",
            "loss",
            members=[world.sweep_ids[HOB]],
        )
        return str(record.id)

    @staticmethod
    def _index_page(world):
        return workspace.investigations_index_page(
            world.service, PROJECT, time.time_ns()
        )

    @staticmethod
    def _index_names(page) -> set[str]:
        return {
            node.children
            for node in _components(page)
            if isinstance(node, html.Span)
            and getattr(node, "className", None) == "sfx"
            and node.children
        }

    def test_index_rows_show_names_factors_outcomes_and_coverage(
        self, world, pending_archive
    ):
        page = self._index_page(world)
        names = self._index_names(page)
        assert names == {
            MAIN_INV,
            "agent-curated",
            "atlas-flagged-only",
            "atlas-archived-pair",
            "atlas-width-mix",
            "atlas-pending-archive",
        }
        text = _page_text(page)
        for row in world.service.investigations_index(PROJECT):
            assert (
                f"{row.with_outcome} with outcome · "
                f"{row.member_count - row.completed} incomplete · "
                f"{row.invalid} invalid" in text
            )
            assert any(
                isinstance(node, html.A)
                and node.href
                and node.href.endswith(f"/investigation/{row.investigation_id}")
                for node in _components(page)
            )
            assert any(
                isinstance(node, html.A)
                and node.href
                and node.href.endswith(f"/investigation/{row.investigation_id}/edit")
                for node in _components(page)
            )

    def test_every_sweep_organized_leaves_unorganized_empty(
        self, world, pending_archive
    ):
        page = self._index_page(world)
        assert "not in any Investigation" in _page_text(page)
        assert world.service.unorganized(PROJECT) == []

    def test_archive_hides_from_the_default_index_and_restore_brings_back(
        self, world, pending_archive
    ):
        world.service.archive_investigation(pending_archive)
        page = self._index_page(world)
        assert "atlas-pending-archive" not in self._index_names(page)
        archived = world.service.investigations_index(PROJECT, include_archived=True)
        assert "atlas-pending-archive" in {row.name for row in archived}
        assert world.service.unorganized(PROJECT) == []

        world.service.restore_investigation(pending_archive)
        page = self._index_page(world)
        assert "atlas-pending-archive" in self._index_names(page)

    def test_edit_members_page_marks_saved_membership(self, world, saved):
        pathname = saved["url"]["pathname"]
        investigation_id = pathname.rsplit("/", 1)[1]
        detail = world.service.investigation_detail(investigation_id)
        page, _polls = page_content(f"{_inv_url(investigation_id)}/edit", world.service)
        saved_ids = {str(member) for member in detail.investigation.members}
        for node in _pattern(page, "inv-edit-pick"):
            sweep_id = node.id["inv-edit-pick"]
            expected = [sweep_id] if sweep_id in saved_ids else []
            assert node.value == expected, sweep_id
        save_button = _by_id(page, {"inv-edit-save": "save"})
        assert save_button is not None
