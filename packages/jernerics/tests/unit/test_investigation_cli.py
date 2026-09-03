"""Investigation CLI over TrackingClient, exercised against a real
in-process server.

Seeds a temp Store through IngestService (one project, two completed
sweeps, each with a completed trial, a manual categorical param, and a
scalar outcome), then drives the Typer CLI through CliRunner with the
client riding an ASGI transport — the same pattern as the tracking CLI
tests.
"""

import asyncio
import json
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from jernerics.cli import app as cli_app
from jernerics.tracking import ProjectHandle, TrackingClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ExecutionEndEvent,
    ExecutionOutcome,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    InvestigationRecord,
    ManualParamEvent,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    decode_selection,
    sweep_id_for,
)
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.store import Store
from typer.testing import CliRunner

PROJECT = "inv-cli"
runner = CliRunner()


def _at(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


class SyncASGITransport(httpx.BaseTransport):
    """Minimal sync ASGI transport: one asyncio.run per request."""

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

    def apply(self, events: list) -> None:
        self.ingest.apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
        )


def _seed_events(s: Scenario) -> list:
    trial_a = uuid.uuid4()
    trial_b = uuid.uuid4()
    ex_a = uuid.uuid4()
    ex_b = uuid.uuid4()
    events: list = []
    for sweep_id, name in ((s.sweep_a, "alpha"), (s.sweep_b, "beta")):
        events.append(
            SweepSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-5000),
                project=PROJECT,
                sweep_id=sweep_id,
                name=name,
                state="completed",
            )
        )
        events.append(
            SubmissionSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-4950),
                submission_id=uuid.uuid4(),
                sweep_id=sweep_id,
                backend="local",
                state=SubmissionState.COMPLETED,
                submitted_at=_at(-4940),
                expected_trials=1,
                git_hash="a" * 40,
                config_source="config.py",
            )
        )
    events.extend(
        [
            TrialSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-4000),
                trial_id=trial_a,
                sweep_id=s.sweep_a,
                number=0,
                state=TrialState.COMPLETED,
                params=FlatContext(root={"lr": 0.1}),
                objective=0.5,
                retry_root_trial_id=trial_a,
            ),
            TrialSnapshotEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-3000),
                trial_id=trial_b,
                sweep_id=s.sweep_b,
                number=0,
                state=TrialState.COMPLETED,
                params=FlatContext(root={"lr": 0.2}),
                objective=0.75,
                retry_root_trial_id=trial_b,
            ),
            ExecutionStartEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-3900),
                execution_id=ex_a,
                trial_id=trial_a,
                hostname="node01",
                started_at=_at(-3900),
            ),
            ExecutionEndEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-3800),
                execution_id=ex_a,
                ended_at=_at(-3800),
                outcome=ExecutionOutcome.SUCCESS,
                exit_code=0,
            ),
            ExecutionStartEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-2900),
                execution_id=ex_b,
                trial_id=trial_b,
                hostname="node02",
                started_at=_at(-2900),
            ),
            ExecutionEndEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-2800),
                execution_id=ex_b,
                ended_at=_at(-2800),
                outcome=ExecutionOutcome.SUCCESS,
                exit_code=0,
            ),
            ValueEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-3790),
                trial_id=trial_a,
                key="rmse",
                step=0,
                value=0.5,
            ),
            ValueEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-2790),
                trial_id=trial_b,
                key="rmse",
                step=0,
                value=0.75,
            ),
            ManualParamEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-3700),
                trial_id=trial_a,
                key="optimizer",
                value="adam",
            ),
            ManualParamEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-2700),
                trial_id=trial_b,
                key="optimizer",
                value="sgd",
            ),
        ]
    )
    return events


def _build_scenario(tmp_path) -> Scenario:
    store = Store(tmp_path / "inv.sqlite")
    app = create_app(store, artifacts_root=tmp_path / "artifacts")
    ingest = IngestService(store, artifacts_root=tmp_path / "artifacts")
    s = Scenario(
        store=store,
        app=app,
        ingest=ingest,
        transport=SyncASGITransport(app=app),
        sweep_a=sweep_id_for(PROJECT, "alpha"),
        sweep_b=sweep_id_for(PROJECT, "beta"),
    )
    s.apply(_seed_events(s))
    return s


def _write_workspace(tmp_path, tracking_server: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{PROJECT}"\n'
        "\n"
        "[tool.jernerics]\n"
        f'tracking_server = "{tracking_server}"\n'
    )


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    s = _build_scenario(tmp_path)
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


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    monkeypatch.setenv("COLUMNS", "240")


def _record(
    name: str,
    investigation_id: uuid.UUID | None = None,
    *,
    archived_ns: int | None = None,
) -> InvestigationRecord:
    return InvestigationRecord(
        id=investigation_id or uuid.uuid4(),
        project=PROJECT,
        name=name,
        factor="optimizer",
        outcome="rmse",
        replicate_factor=None,
        archived_ns=archived_ns,
        created_ns=1,
        updated_ns=1,
        members=(),
    )


def _create(
    s: Scenario,
    name: str,
    *,
    sweeps: list[uuid.UUID] | None = None,
    factor: str = "optimizer",
    outcome: str = "rmse",
    replicate: str | None = None,
) -> InvestigationRecord:
    with TrackingClient("http://testserver", transport=s.transport) as client:
        return client.project(PROJECT).create_investigation(
            name,
            factor,
            outcome,
            members=sweeps or [],
            replicate_factor=replicate,
        )


class TestCreateCommand:
    def test_json_shape(self, scenario):
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "create",
                "heat",
                "--factor",
                "optimizer",
                "--outcome",
                "rmse",
                "--replicate-factor",
                "seed",
                str(scenario.sweep_a),
                str(scenario.sweep_b),
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["name"] == "heat"
        assert payload["project"] == PROJECT
        assert payload["factor"] == "optimizer"
        assert payload["outcome"] == "rmse"
        assert payload["replicate_factor"] == "seed"
        assert sorted(payload["members"]) == sorted(
            [str(scenario.sweep_a), str(scenario.sweep_b)]
        )
        assert payload["archived_ns"] is None
        assert isinstance(payload["id"], str)
        assert isinstance(payload["created_ns"], int)
        assert payload["updated_ns"] >= payload["created_ns"]

    def test_human_confirmation(self, scenario):
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "create",
                "heat",
                "--factor",
                "optimizer",
                "--outcome",
                "rmse",
                str(scenario.sweep_a),
                str(scenario.sweep_b),
            ],
        )
        assert result.exit_code == 0
        assert "Created investigation 'heat' (2 member sweeps)" in result.output

    def test_idempotent_retry_returns_same_record(self, scenario):
        argv = [
            "investigation",
            "create",
            "heat",
            "--factor",
            "optimizer",
            "--outcome",
            "rmse",
            str(scenario.sweep_a),
            "--json",
        ]
        first = json.loads(runner.invoke(cli_app, argv).output)
        second = json.loads(runner.invoke(cli_app, argv).output)
        assert first["id"] == second["id"]
        assert first["members"] == [str(scenario.sweep_a)]

    def test_conflicting_body_fails(self, scenario):
        _create(scenario, "heat")
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "create",
                "heat",
                "--factor",
                "other",
                "--outcome",
                "rmse",
                "--json",
            ],
        )
        assert result.exit_code == 1
        assert "investigation_conflict" in result.output

    def test_unknown_sweep_fails(self, scenario):
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "create",
                "heat",
                "--factor",
                "optimizer",
                "--outcome",
                "rmse",
                str(uuid.uuid4()),
                "--json",
            ],
        )
        assert result.exit_code == 1
        assert "sweep_not_found" in result.output


class TestListCommand:
    def test_json_lists_records(self, scenario):
        created = [_create(scenario, "heat"), _create(scenario, "cool")]
        result = runner.invoke(cli_app, ["investigation", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        by_name = {record["name"]: record for record in payload}
        assert set(by_name) == {"heat", "cool"}
        for record in created:
            dumped = by_name[record.name]
            assert dumped["id"] == str(record.id)
            assert dumped["factor"] == record.factor
            assert dumped["outcome"] == record.outcome
            assert dumped["members"] == [str(s) for s in record.members]
            assert dumped["archived_ns"] is None

    def test_human_table_lists_names(self, scenario):
        _create(scenario, "heat")
        result = runner.invoke(cli_app, ["investigation", "list"])
        assert result.exit_code == 0
        assert "heat" in result.output
        assert "optimizer" in result.output
        assert "rmse" in result.output

    def test_empty_project(self, scenario):
        result = runner.invoke(cli_app, ["investigation", "list"])
        assert result.exit_code == 0
        assert "No investigations found." in result.output
        result = runner.invoke(cli_app, ["investigation", "list", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == []

    def test_archived_hidden_by_default(self, scenario):
        record = _create(scenario, "heat")
        with TrackingClient(
            "http://testserver", transport=scenario.transport
        ) as client:
            client.project(PROJECT).archive_investigation(record.id)
        result = runner.invoke(cli_app, ["investigation", "list", "--json"])
        assert json.loads(result.output) == []
        result = runner.invoke(
            cli_app, ["investigation", "list", "--include-archived", "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert len(payload) == 1
        assert payload[0]["archived_ns"] is not None


class TestShowCommand:
    def test_json_by_name_with_selection_token(self, scenario):
        _create(scenario, "heat", sweeps=[scenario.sweep_a, scenario.sweep_b])
        result = runner.invoke(cli_app, ["investigation", "show", "heat", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert set(payload) == {"investigation", "coverage", "selection"}
        assert payload["investigation"]["name"] == "heat"
        assert payload["investigation"]["members"] == [
            str(scenario.sweep_a),
            str(scenario.sweep_b),
        ]
        coverage = payload["coverage"]
        assert coverage["members"] == 2
        assert coverage["with_outcome"] == 2
        assert coverage["completed"] == 2
        assert coverage["invalid"] == 0
        assert isinstance(coverage["last_activity_ns"], int)
        selection = payload["selection"]
        assert selection["project"] == PROJECT
        assert sorted(selection["sweeps"]) == sorted(
            [str(scenario.sweep_a), str(scenario.sweep_b)]
        )
        decoded = decode_selection(selection["token"])
        assert decoded.project == PROJECT
        assert set(decoded.sweeps or ()) == {scenario.sweep_a, scenario.sweep_b}

    def test_json_by_id(self, scenario):
        record = _create(scenario, "heat", sweeps=[scenario.sweep_a])
        result = runner.invoke(
            cli_app, ["investigation", "show", str(record.id), "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["investigation"]["id"] == str(record.id)

    def test_human_shows_coverage_and_token(self, scenario):
        _create(scenario, "heat", sweeps=[scenario.sweep_a, scenario.sweep_b])
        result = runner.invoke(cli_app, ["investigation", "show", "heat"])
        assert result.exit_code == 0
        assert "heat" in result.output
        assert "with outcome: 2" in result.output
        assert "completed: 2" in result.output
        assert "selection token: " in result.output

    def test_unknown_name_lists_known(self, scenario):
        _create(scenario, "heat")
        result = runner.invoke(cli_app, ["investigation", "show", "nope"])
        assert result.exit_code == 1
        assert "no investigation named 'nope'" in result.output
        assert "heat" in result.output

    def test_unknown_id_fails(self, scenario):
        result = runner.invoke(
            cli_app, ["investigation", "show", str(uuid.uuid4()), "--json"]
        )
        assert result.exit_code == 1
        assert "investigation_not_found" in result.output

    def test_ambiguous_name_fails(self, scenario, monkeypatch):
        duplicate = [_record("dupe"), _record("dupe")]
        monkeypatch.setattr(
            ProjectHandle,
            "investigations",
            lambda self, *, include_archived=False: duplicate,
        )
        result = runner.invoke(cli_app, ["investigation", "show", "dupe"])
        assert result.exit_code == 1
        assert "ambiguous" in result.output


class TestPreviewCommand:
    def test_json_candidates(self, scenario):
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "preview",
                str(scenario.sweep_a),
                str(scenario.sweep_b),
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["project"] == PROJECT
        assert payload["member_count"] == 2
        assert payload["factors"][0] == {
            "kind": "manual_param",
            "name": "optimizer",
            "members": 2,
        }
        assert payload["outcomes"] == [{"key": "rmse", "members": 2}]
        assert payload["warnings"] == []

    def test_unknown_sweep_is_warning_not_error(self, scenario):
        unknown = uuid.uuid4()
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "preview",
                str(unknown),
                str(scenario.sweep_a),
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["member_count"] == 1
        assert payload["warnings"] == [
            {
                "kind": "unknown_sweep",
                "detail": f"no sweep with id {unknown}",
            }
        ]

    def test_human_shows_candidates(self, scenario):
        result = runner.invoke(
            cli_app,
            ["investigation", "preview", str(scenario.sweep_a), str(scenario.sweep_b)],
        )
        assert result.exit_code == 0
        assert "Preview over 2 candidate sweeps" in result.output
        assert "optimizer" in result.output
        assert "rmse" in result.output


class TestMembersCommand:
    def test_set_add_remove_json(self, scenario):
        created = _create(scenario, "heat", sweeps=[scenario.sweep_a])
        ref = str(created.id)
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "members",
                "add",
                ref,
                str(scenario.sweep_b),
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert set(payload["members"]) == {
            str(scenario.sweep_a),
            str(scenario.sweep_b),
        }

        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "members",
                "remove",
                "heat",
                str(scenario.sweep_a),
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["members"] == [str(scenario.sweep_b)]

        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "members",
                "set",
                "heat",
                str(scenario.sweep_a),
                str(scenario.sweep_b),
                "--json",
            ],
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert set(payload["members"]) == {
            str(scenario.sweep_a),
            str(scenario.sweep_b),
        }

    def test_human_reports_new_count(self, scenario):
        created = _create(scenario, "heat", sweeps=[scenario.sweep_a])
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "members",
                "add",
                str(created.id),
                str(scenario.sweep_b),
            ],
        )
        assert result.exit_code == 0
        assert "now 2 member sweeps" in result.output

    def test_unknown_sweep_fails(self, scenario):
        created = _create(scenario, "heat")
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "members",
                "add",
                str(created.id),
                str(uuid.uuid4()),
            ],
        )
        assert result.exit_code == 1
        assert "sweep_not_found" in result.output

    def test_unknown_ref_fails(self, scenario):
        result = runner.invoke(
            cli_app,
            [
                "investigation",
                "members",
                "add",
                "nope",
                str(scenario.sweep_a),
            ],
        )
        assert result.exit_code == 1
        assert "no investigation named 'nope'" in result.output


class TestArchiveRestoreCommand:
    def test_json_round_trip(self, scenario):
        created = _create(scenario, "heat")
        result = runner.invoke(cli_app, ["investigation", "archive", "heat", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["id"] == str(created.id)
        assert payload["archived_ns"] is not None

        result = runner.invoke(
            cli_app, ["investigation", "restore", str(created.id), "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["archived_ns"] is None

    def test_restore_by_name_on_archived_record(self, scenario):
        created = _create(scenario, "heat")
        with TrackingClient(
            "http://testserver", transport=scenario.transport
        ) as client:
            client.project(PROJECT).archive_investigation(created.id)
        result = runner.invoke(cli_app, ["investigation", "restore", "heat", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output)["archived_ns"] is None

    def test_human_messages(self, scenario):
        _create(scenario, "heat")
        result = runner.invoke(cli_app, ["investigation", "archive", "heat"])
        assert result.exit_code == 0
        assert "Archived 'heat'" in result.output
        result = runner.invoke(cli_app, ["investigation", "restore", "heat"])
        assert result.exit_code == 0
        assert "Restored 'heat'" in result.output
