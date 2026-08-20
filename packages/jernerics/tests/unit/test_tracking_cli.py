"""Observability CLI over TrackingClient, exercised against a real
in-process server.

Seeds a temp Store through IngestService (one project, two sweeps, a
three-generation retry chain, a multi-execution trial, scalar and JSON
values, sampled and manual params, uploaded and declared-only artifacts,
provenance), then drives the Typer CLI through CliRunner with the client
riding an ASGI transport — the same pattern as the h5d.9 client tests.
"""

import asyncio
import hashlib
import json
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import ANY

import httpx
import pytest
from fastapi import FastAPI
from jernerics.cli import app as cli_app
from jernerics.tracking import TrackingClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionOutcome,
    ExecutionStartEvent,
    FailureKind,
    FlatContext,
    IngestRequest,
    ManualParamEvent,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    sweep_id_for,
)
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.store import Store
from typer.testing import CliRunner

PROJECT = "obs-cli"
JSON_BODY = b'{"a": 1, "b": [true, null]}'
RAW_BODY = b"raw-artifact-bytes"
runner = CliRunner()


def _at(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


class SyncASGITransport(httpx.BaseTransport):
    """Minimal sync ASGI transport: one asyncio.run per request.

    The ASGI receive/send doubles must be coroutines without awaiting,
    by protocol; the app consumes them inside asyncio.run below.
    """

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": request.method,
            "scheme": "http",
            "path": request.url.path,
            "raw_path": request.url.raw_path,
            "query_string": request.url.query,
            "root_path": "",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "headers": [(k.lower(), v) for k, v in request.headers.raw],
        }
        started: dict[str, Any] = {}
        collected = bytearray()

        async def receive() -> dict[str, Any]:  # noqa: RUF029
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: MutableMapping[str, Any]) -> None:  # noqa: RUF029
            if message["type"] == "http.response.start":
                started.update(message)
            elif message["type"] == "http.response.body":
                collected.extend(message.get("body", b""))

        asyncio.run(self.app(scope, receive, send))
        return httpx.Response(
            started["status"],
            headers=started.get("headers", []),
            content=bytes(collected),
        )


@dataclass
class Scenario:
    store: Store
    app: FastAPI
    ingest: IngestService
    transport: SyncASGITransport
    sweep_a: uuid.UUID
    sweep_b: uuid.UUID
    t_root: uuid.UUID
    t_retry: uuid.UUID
    t_retry2: uuid.UUID
    u_trial: uuid.UUID
    ex_root: uuid.UUID
    ex_retry: uuid.UUID
    ex_retry2: uuid.UUID
    ex_u1: uuid.UUID
    ex_u2: uuid.UUID
    art_report: uuid.UUID
    art_model: uuid.UUID
    art_log: uuid.UUID

    def apply(self, events: list) -> None:
        self.ingest.apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
        )


def _seed_events(s: Scenario) -> list:
    return [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-5000),
            project=PROJECT,
            sweep_id=s.sweep_a,
            name="alpha",
            state="running",
        ),
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-4900),
            project=PROJECT,
            sweep_id=s.sweep_b,
            name="beta",
            state="completed",
        ),
        SubmissionSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-4950),
            submission_id=uuid.uuid4(),
            sweep_id=s.sweep_a,
            backend="local",
            state=SubmissionState.COMPLETED,
            submitted_at=_at(-4940),
            expected_trials=3,
            git_hash="a" * 40,
            config_source="config.py",
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-4000),
            trial_id=s.t_root,
            sweep_id=s.sweep_a,
            number=0,
            state=TrialState.RUNNING,
            params=FlatContext(root={"lr": 0.1}),
            retry_root_trial_id=s.t_root,
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-2400),
            trial_id=s.t_retry,
            sweep_id=s.sweep_a,
            number=1,
            state=TrialState.COMPLETED,
            params=FlatContext(root={"lr": 0.05}),
            objective=0.5,
            retry_of_trial_id=s.t_root,
            retry_root_trial_id=s.t_root,
            retry_index=1,
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1800),
            trial_id=s.t_retry2,
            sweep_id=s.sweep_a,
            number=2,
            state=TrialState.FAILED,
            retry_of_trial_id=s.t_retry,
            retry_root_trial_id=s.t_root,
            retry_index=2,
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1200),
            trial_id=s.u_trial,
            sweep_id=s.sweep_b,
            number=0,
            state=TrialState.RUNNING,
            retry_root_trial_id=s.u_trial,
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-3600),
            execution_id=s.ex_root,
            trial_id=s.t_root,
            hostname="node01",
            started_at=_at(-3600),
        ),
        ExecutionHeartbeatEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1000),
            execution_id=s.ex_root,
            at=_at(-1000),
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-2300),
            execution_id=s.ex_retry,
            trial_id=s.t_retry,
            hostname="node02",
            started_at=_at(-2300),
        ),
        ExecutionEndEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-2000),
            execution_id=s.ex_retry,
            ended_at=_at(-2000),
            outcome=ExecutionOutcome.SUCCESS,
            exit_code=0,
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1700),
            execution_id=s.ex_retry2,
            trial_id=s.t_retry2,
            hostname="node03",
            started_at=_at(-1700),
        ),
        ExecutionEndEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1600),
            execution_id=s.ex_retry2,
            ended_at=_at(-1600),
            outcome=ExecutionOutcome.FAILURE,
            exit_code=1,
            failure_kind=FailureKind.EXCEPTION,
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1100),
            execution_id=s.ex_u1,
            trial_id=s.u_trial,
            hostname="node04",
            started_at=_at(-1100),
        ),
        ExecutionEndEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1000),
            execution_id=s.ex_u1,
            ended_at=_at(-1000),
            outcome=ExecutionOutcome.SUCCESS,
            exit_code=0,
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-500),
            execution_id=s.ex_u2,
            trial_id=s.u_trial,
            hostname="node05",
            started_at=_at(-500),
        ),
        ExecutionHeartbeatEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-5),
            execution_id=s.ex_u2,
            at=_at(-5),
        ),
        *[
            ValueEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-60 + step),
                trial_id=s.t_root,
                key="loss",
                step=step,
                value=3.0 - step,
            )
            for step in range(3)
        ],
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-50),
            trial_id=s.t_retry,
            key="loss",
            step=0,
            value=0.5,
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-49),
            trial_id=s.t_retry,
            key="final_loss",
            step=0,
            value=0.25,
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-48),
            trial_id=s.t_retry,
            key="snapshot",
            step=0,
            observation={"curve": [1, 2]},
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-30),
            trial_id=s.u_trial,
            key="loss",
            step=0,
            value=7.0,
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-29),
            trial_id=s.u_trial,
            key="beta-only",
            step=0,
            observation={"kind": "beta"},
        ),
        ManualParamEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-3000),
            trial_id=s.t_root,
            key="note",
            value="hello",
        ),
        ManualParamEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1100),
            trial_id=s.u_trial,
            key="seed",
            value=42,
        ),
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1500),
            artifact_id=s.art_report,
            trial_id=s.t_retry,
            execution_id=s.ex_retry,
            key="report",
            filename="report.json",
            content_type="application/json",
            size_bytes=len(JSON_BODY),
            sha256=hashlib.sha256(JSON_BODY).hexdigest(),
        ),
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1400),
            artifact_id=s.art_model,
            trial_id=s.t_root,
            execution_id=s.ex_root,
            key="model",
            filename="model.bin",
            content_type="application/octet-stream",
            size_bytes=len(RAW_BODY),
            sha256=hashlib.sha256(RAW_BODY).hexdigest(),
        ),
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1300),
            artifact_id=s.art_log,
            trial_id=s.t_retry2,
            key="log",
            filename="run.log",
            content_type="text/plain",
            size_bytes=3,
            sha256=hashlib.sha256(b"xyz").hexdigest(),
            source="system",
        ),
    ]


def _build_scenario(tmp_path, *, api_key: str | None = None) -> Scenario:
    store = Store(tmp_path / "obs.sqlite")
    artifacts_root = tmp_path / "artifacts"
    app = create_app(store, artifacts_root=artifacts_root, api_key=api_key)
    ingest = IngestService(store, artifacts_root=artifacts_root)
    s = Scenario(
        store=store,
        app=app,
        ingest=ingest,
        transport=SyncASGITransport(app=app),
        sweep_a=sweep_id_for(PROJECT, "alpha"),
        sweep_b=sweep_id_for(PROJECT, "beta"),
        t_root=uuid.uuid4(),
        t_retry=uuid.uuid4(),
        t_retry2=uuid.uuid4(),
        u_trial=uuid.uuid4(),
        ex_root=uuid.uuid4(),
        ex_retry=uuid.uuid4(),
        ex_retry2=uuid.uuid4(),
        ex_u1=uuid.uuid4(),
        ex_u2=uuid.uuid4(),
        art_report=uuid.uuid4(),
        art_model=uuid.uuid4(),
        art_log=uuid.uuid4(),
    )
    s.apply(_seed_events(s))
    return s


def _upload(s: Scenario) -> None:
    with httpx.Client(
        transport=SyncASGITransport(app=s.app), base_url="http://t"
    ) as uploader:
        for artifact_id, body in ((s.art_report, JSON_BODY), (s.art_model, RAW_BODY)):
            response = uploader.put(f"/artifact/{artifact_id}", content=body)
            assert response.status_code == 200


def _write_workspace(tmp_path, tracking_server: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[project]\n"
        'name = "obs-cli"\n'
        "\n"
        "[tool.jernerics]\n"
        f'tracking_server = "{tracking_server}"\n'
    )


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    s = _build_scenario(tmp_path)
    _upload(s)
    _write_workspace(tmp_path, "http://testserver")
    monkeypatch.chdir(tmp_path / "workspace")
    monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "http://testserver")
    monkeypatch.setattr(
        TrackingClient,
        "from_env",
        classmethod(
            lambda cls, timeout=30.0: TrackingClient(
                "http://testserver", transport=s.transport
            )
        ),
    )
    yield s
    s.store.close()


@pytest.fixture
def wrong_key_scenario(tmp_path, monkeypatch):
    s = _build_scenario(tmp_path, api_key="secret")
    _write_workspace(tmp_path, "http://testserver")
    monkeypatch.chdir(tmp_path / "workspace")
    monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "http://testserver")
    monkeypatch.setattr(
        TrackingClient,
        "from_env",
        classmethod(
            lambda cls, timeout=30.0: TrackingClient(
                "http://testserver", transport=s.transport, api_key="wrong-key"
            )
        ),
    )
    yield s
    s.store.close()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    monkeypatch.setenv("COLUMNS", "240")


def _trial_dump(
    trial_id: uuid.UUID,
    sweep_id: uuid.UUID,
    number: int,
    state: str,
    *,
    objective: float | None = None,
    retry_of: uuid.UUID | None = None,
    root: uuid.UUID | None = None,
    retry_index: int = 0,
) -> dict:
    return {
        "trial_id": str(trial_id),
        "sweep_id": str(sweep_id),
        "number": number,
        "state": state,
        "params": {},
        "objective": objective,
        "distributions": None,
        "attrs": None,
        "retry_of_trial_id": str(retry_of) if retry_of else None,
        "retry_root_trial_id": str(root or trial_id),
        "retry_index": retry_index,
    }


class TestRunsCommand:
    def test_json_is_exact_trial_records_plus_monitoring(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "runs", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == [
            {
                **_trial_dump(
                    scenario.t_root,
                    scenario.sweep_a,
                    0,
                    "running",
                ),
                "monitoring": "stale",
            },
            {
                **_trial_dump(
                    scenario.t_retry,
                    scenario.sweep_a,
                    1,
                    "completed",
                    objective=0.5,
                    retry_of=scenario.t_root,
                    root=scenario.t_root,
                    retry_index=1,
                ),
                "monitoring": "ended",
            },
            {
                **_trial_dump(
                    scenario.t_retry2,
                    scenario.sweep_a,
                    2,
                    "failed",
                    retry_of=scenario.t_retry,
                    root=scenario.t_root,
                    retry_index=2,
                ),
                "monitoring": "ended",
            },
            {
                **_trial_dump(scenario.u_trial, scenario.sweep_b, 0, "running"),
                "monitoring": "active",
            },
        ]

    def test_json_types_are_plain(self, scenario):
        payload = json.loads(
            runner.invoke(cli_app, ["tracking", "runs", "--json"]).output
        )
        for row in payload:
            assert isinstance(row["trial_id"], str)
            assert isinstance(row["sweep_id"], str)
            assert isinstance(row["number"], int)
            assert row["state"] in {
                "waiting",
                "running",
                "completed",
                "failed",
                "pruned",
            }
            assert row["monitoring"] in {
                "active",
                "quiet",
                "stale",
                "ended",
                "unknown",
            }
            assert isinstance(row["retry_index"], int)

    def test_table_renders_sweeps_states_and_lineage_columns(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "runs"])
        assert result.exit_code == 0
        out = result.output
        assert "alpha" in out
        assert "beta" in out
        assert "running" in out
        assert "completed" in out
        assert "failed" in out
        assert "stale" in out
        assert "ended" in out
        assert "active" in out
        assert "RETRY" in out
        assert "ROOT" in out
        assert "alpha:0" in out


class TestSummaryCommand:
    def test_by_sweep_number_json_is_exact(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "summary", "alpha:1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {
            "trial": _trial_dump(
                scenario.t_retry,
                scenario.sweep_a,
                1,
                "completed",
                objective=0.5,
                retry_of=scenario.t_root,
                root=scenario.t_root,
                retry_index=1,
            ),
            "sweep": "alpha",
            "label": "alpha:1",
            "lineage": [
                {
                    "trial_id": str(scenario.t_root),
                    "retry_of_trial_id": None,
                    "retry_root_trial_id": str(scenario.t_root),
                    "retry_index": 0,
                    "number": 0,
                    "sweep_id": str(scenario.sweep_a),
                },
                {
                    "trial_id": str(scenario.t_retry),
                    "retry_of_trial_id": str(scenario.t_root),
                    "retry_root_trial_id": str(scenario.t_root),
                    "retry_index": 1,
                    "number": 1,
                    "sweep_id": str(scenario.sweep_a),
                },
                {
                    "trial_id": str(scenario.t_retry2),
                    "retry_of_trial_id": str(scenario.t_retry),
                    "retry_root_trial_id": str(scenario.t_root),
                    "retry_index": 2,
                    "number": 2,
                    "sweep_id": str(scenario.sweep_a),
                },
            ],
            "params": [
                {
                    "trial_id": str(scenario.t_retry),
                    "kind": "sampled",
                    "key": "lr",
                    "value": 0.05,
                }
            ],
            "values": [
                {
                    "key": "final_loss",
                    "kind": "scalar",
                    "n_points": 1,
                    "latest_step": 0,
                    "n_trials": 1,
                },
                {
                    "key": "loss",
                    "kind": "scalar",
                    "n_points": 1,
                    "latest_step": 0,
                    "n_trials": 1,
                },
                {
                    "key": "snapshot",
                    "kind": "json",
                    "n_points": 1,
                    "latest_step": 0,
                    "n_trials": 1,
                },
            ],
            "artifacts": [
                {
                    "artifact_id": str(scenario.art_report),
                    "trial_id": str(scenario.t_retry),
                    "execution_id": str(scenario.ex_retry),
                    "key": "report",
                    "filename": "report.json",
                    "content_type": "application/json",
                    "size_bytes": len(JSON_BODY),
                    "sha256": hashlib.sha256(JSON_BODY).hexdigest(),
                    "context": None,
                    "source": "user",
                    "received_ns": ANY,
                }
            ],
            "executions": [
                {
                    "execution_id": str(scenario.ex_retry),
                    "trial_id": str(scenario.t_retry),
                    "hostname": "node02",
                    "started_at": ANY,
                    "ended_at": ANY,
                    "outcome": "success",
                    "exit_code": 0,
                    "failure_kind": None,
                    "last_heartbeat_ns": None,
                    "last_observation_ns": ANY,
                    "monitoring": "ended",
                }
            ],
        }

    def test_by_hex_id_renders_labeled_params_and_artifact_state(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "summary", str(scenario.t_root)])
        assert result.exit_code == 0
        out = result.output
        assert "Trial alpha:0" in out
        assert "state: running" in out
        assert "note" in out
        assert "manual" in out
        assert "lr" in out
        assert "sampled" in out
        assert "model.bin" in out
        assert "no" in out

    def test_uploaded_artifact_shows_received(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "summary", "alpha:1"])
        assert result.exit_code == 0
        out = result.output
        assert "report.json" in out
        assert "yes" in out

    def test_mid_chain_summary_names_parent_and_root_separately_from_executions(
        self, scenario
    ):
        result = runner.invoke(cli_app, ["tracking", "summary", "alpha:1", "--json"])
        data = json.loads(result.output)
        generations = {record["trial_id"]: record for record in data["lineage"]}
        assert len(generations) == 3
        mid = data["trial"]
        assert mid["trial_id"] == str(scenario.t_retry)
        assert mid["retry_of_trial_id"] == str(scenario.t_root)
        assert mid["retry_root_trial_id"] == str(scenario.t_root)
        assert all("outcome" not in record for record in data["lineage"])
        assert all("retry_index" not in ex for ex in data["executions"])

    def test_multi_execution_trial_lists_each_execution_with_monitoring(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "summary", "beta:0", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert [ex["hostname"] for ex in data["executions"]] == ["node04", "node05"]
        assert [ex["monitoring"] for ex in data["executions"]] == ["ended", "active"]
        assert [ex["outcome"] for ex in data["executions"]] == ["success", None]

    def test_text_renders_all_sections(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "summary", "beta:0"])
        assert result.exit_code == 0
        out = result.output
        assert "Retry lineage (1 generations" in out
        assert "Params (1)" in out
        assert "seed" in out
        assert "manual" in out
        assert "Values (2)" in out
        assert "beta-only" in out
        assert "Executions (2)" in out
        assert "node04" in out
        assert "node05" in out


class TestDiffCommand:
    def test_json_is_exact_union(self, scenario):
        result = runner.invoke(
            cli_app, ["tracking", "diff", "alpha:1", "beta:0", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {
            "a": _trial_dump(
                scenario.t_retry,
                scenario.sweep_a,
                1,
                "completed",
                objective=0.5,
                retry_of=scenario.t_root,
                root=scenario.t_root,
                retry_index=1,
            ),
            "b": _trial_dump(scenario.u_trial, scenario.sweep_b, 0, "running"),
            "a_label": "alpha:1",
            "b_label": "beta:0",
            "params": [
                {"key": "lr", "a": 0.05, "b": None},
                {"key": "seed", "a": None, "b": 42},
            ],
            "values": [
                {"key": "beta-only", "a": None, "b": '{"kind":"beta"}'},
                {"key": "final_loss", "a": 0.25, "b": None},
                {"key": "loss", "a": 0.5, "b": 7.0},
                {"key": "snapshot", "a": '{"curve":[1,2]}', "b": None},
            ],
            "objective": {"a": 0.5, "b": None},
        }

    def test_text_marks_missing_sides(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "diff", "alpha:1", "beta:0"])
        assert result.exit_code == 0
        out = result.output
        assert "A: alpha:1 (completed)" in out
        assert "B: beta:0 (running)" in out
        assert "(missing)" in out
        assert "hello" not in out
        assert "Objective" in out


class TestTraceCommand:
    def test_scalar_series_json_is_exact(self, scenario):
        result = runner.invoke(
            cli_app, ["tracking", "trace", "alpha:0", "loss", "--json"]
        )
        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "trial_id": str(scenario.t_root),
            "label": "alpha:0",
            "key": "loss",
            "series": [
                {"step": 0, "value": 3.0},
                {"step": 1, "value": 2.0},
                {"step": 2, "value": 1.0},
            ],
        }

    def test_json_observation_renders_as_compact_string(self, scenario):
        result = runner.invoke(
            cli_app, ["tracking", "trace", "alpha:1", "snapshot", "--json"]
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["series"] == [{"step": 0, "value": '{"curve":[1,2]}'}]

    def test_text_series(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "trace", "alpha:0", "loss"])
        assert result.exit_code == 0
        out = result.output
        assert "Trace: alpha:0 / loss (3 points)" in out
        assert "step 0: 3" in out
        assert "step 2: 1" in out

    def test_unknown_key_is_a_clean_error(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "trace", "alpha:0", "ghost"])
        assert result.exit_code == 1
        assert "Error: no values for key 'ghost'" in result.output
        assert "Traceback" not in result.output


class TestUnknownTrialRefs:
    @pytest.mark.parametrize(
        "ref", ["ghost:0", "alpha:99", "alpha:notanumber", "alpha"]
    )
    def test_clean_nonzero_exit(self, scenario, ref):
        result = runner.invoke(cli_app, ["tracking", "summary", ref])
        assert result.exit_code == 1
        assert result.output.startswith("Error:")
        assert "Traceback" not in result.output

    def test_unknown_hex_id(self, scenario):
        result = runner.invoke(cli_app, ["tracking", "summary", "f" * 32])
        assert result.exit_code == 1
        assert "no trial with id" in result.output


class TestAuth:
    def test_wrong_key_is_clean_nonzero_exit(self, wrong_key_scenario):
        result = runner.invoke(cli_app, ["tracking", "runs"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "401" in result.output
        assert "not authorized" in result.output
        assert "Traceback" not in result.output


class TestSchemelessUrl:
    def test_actionable_scheme_error(self, tmp_path, monkeypatch):
        _write_workspace(tmp_path, "atlas.example:443")
        monkeypatch.chdir(tmp_path / "workspace")
        monkeypatch.delenv("JERNERICS_TRACKING_SERVER", raising=False)
        monkeypatch.delenv("JERNERICS_API_KEY", raising=False)
        result = runner.invoke(cli_app, ["tracking", "runs"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        assert "scheme" in result.output
        assert "http://" in result.output
        assert "Traceback" not in result.output


class TestQueryCommand:
    def test_raw_query_table(self, scenario):
        result = runner.invoke(
            cli_app,
            ["tracking", "query", "SELECT name, state FROM sweeps ORDER BY name"],
        )
        assert result.exit_code == 0
        out = result.output
        assert "name" in out
        assert "alpha" in out
        assert "beta" in out
        assert "running" in out
        assert "completed" in out

    def test_raw_query_json(self, scenario):
        result = runner.invoke(
            cli_app,
            [
                "tracking",
                "query",
                "SELECT name, state FROM sweeps ORDER BY name",
                "--json",
            ],
        )
        assert result.exit_code == 0
        assert json.loads(result.output) == {
            "columns": ["name", "state"],
            "rows": [["alpha", "running"], ["beta", "completed"]],
        }

    def test_query_is_listed_as_expert_escape_hatch(self):
        tracking = next(
            g.typer_instance for g in cli_app.registered_groups if g.name == "tracking"
        )
        assert tracking is not None
        commands = {c.name: c for c in tracking.registered_commands}
        assert set(commands) == {"replay", "runs", "summary", "diff", "trace", "query"}
        listing = runner.invoke(cli_app, ["tracking", "--help"])
        assert listing.exit_code == 0
        assert "expert escape hatch" in listing.output


class TestNoSqlInRoutineCommands:
    def test_observability_path_imports_client_not_sqlite(self):
        import inspect

        from jernerics.commands import tracking

        source = inspect.getsource(tracking)
        assert "TrackingClient" in source
        assert "sqlite" not in source.lower()
        assert "SELECT" not in source
