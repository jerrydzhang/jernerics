"""Typed TrackingClient acceptance tests over a real in-process server.

Seeds a temp Store through IngestService (two sweeps, a three-generation
retry chain, scalar/JSON/non-step values, sampled+manual params,
uploaded and declared-only artifacts, provenance), then exercises the
client through httpx ASGI transport against the real FastAPI app.
"""

import asyncio
import hashlib
import subprocess
import sys
import uuid
from collections.abc import MutableMapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from jernerics.tracking import TrackingClient, TrackingClientError
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionEndEvent,
    ExecutionOutcome,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    ManualParamEvent,
    Selection,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    decode_selection,
    encode_selection,
    sweep_id_for,
)
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store
from optuna.distributions import FloatDistribution, distribution_to_json
from pydantic import ValidationError

PROJECT = "client-api"
RAW_BODY = b"raw-artifact-bytes"
JSON_BODY = b'{"a": 1, "b": [true, null]}'
SERIES_STEPS = 7
LR_DISTRIBUTION = distribution_to_json(FloatDistribution(0.001, 0.1, log=True))


def _at(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


class SyncASGITransport(httpx.BaseTransport):
    """Minimal sync ASGI transport: one asyncio.run per request.

    httpx's own ASGITransport is async-only; this drives the FastAPI app
    synchronously, the way starlette's TestClient does.
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


class CountingTransport(httpx.BaseTransport):
    """Delegates to an inner transport while counting requests."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner
        self.requests = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return self.inner.handle_request(request)


class FlakyTransport(httpx.BaseTransport):
    """Fails the first N requests with a 500, then delegates."""

    def __init__(self, inner: httpx.BaseTransport, failures: int) -> None:
        self.inner = inner
        self.failures = failures
        self.failed = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.failed < self.failures:
            self.failed += 1
            return httpx.Response(500, text="transient boom")
        return self.inner.handle_request(request)


@dataclass
class Scenario:
    store: Store
    app: FastAPI
    ingest: IngestService
    transport: CountingTransport
    sweep_a: uuid.UUID
    sweep_b: uuid.UUID
    t_root: uuid.UUID
    t_retry: uuid.UUID
    t_retry2: uuid.UUID
    u_trial: uuid.UUID
    ex_root: uuid.UUID
    ex_retry: uuid.UUID
    ex_retry2: uuid.UUID
    ex_u: uuid.UUID
    art_json: uuid.UUID
    art_raw: uuid.UUID
    art_missing: uuid.UUID

    def apply(self, events: list) -> None:
        self.ingest.apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
        )

    def client(self, **kwargs) -> TrackingClient:
        return TrackingClient("http://testserver", transport=self.transport, **kwargs)


def _seed_events(s: Scenario) -> list:
    return [
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-7200),
            project=PROJECT,
            sweep_id=s.sweep_a,
            name="alpha",
            state="running",
        ),
        SweepSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-3600),
            project=PROJECT,
            sweep_id=s.sweep_b,
            name="beta",
            state="completed",
        ),
        SubmissionSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-7100),
            submission_id=uuid.uuid4(),
            sweep_id=s.sweep_a,
            backend="local",
            state=SubmissionState.COMPLETED,
            submitted_at=_at(-7000),
            expected_trials=3,
            git_hash="a" * 40,
            config_source="config.py",
        ),
        SubmissionSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-3500),
            submission_id=uuid.uuid4(),
            sweep_id=s.sweep_b,
            backend="slurm",
            state=SubmissionState.RUNNING,
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-3000),
            trial_id=s.t_root,
            sweep_id=s.sweep_a,
            number=0,
            state=TrialState.RUNNING,
            params=FlatContext(root={"lr": 0.1}),
            distributions=FlatContext(root={"lr": LR_DISTRIBUTION}),
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
            distributions=FlatContext(root={"lr": LR_DISTRIBUTION}),
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
            recorded_at=_at(-2900),
            execution_id=s.ex_root,
            trial_id=s.t_root,
            hostname="node01",
            started_at=_at(-2900),
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-2300),
            execution_id=s.ex_retry,
            trial_id=s.t_retry,
            hostname="node02",
            started_at=_at(-2300),
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1700),
            execution_id=s.ex_retry2,
            trial_id=s.t_retry2,
            hostname="node03",
            started_at=_at(-1700),
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1100),
            execution_id=s.ex_u,
            trial_id=s.u_trial,
            hostname="node04",
            started_at=_at(-1100),
        ),
        ExecutionEndEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-2000),
            execution_id=s.ex_retry,
            ended_at=_at(-2000),
            outcome=ExecutionOutcome.SUCCESS,
            exit_code=0,
        ),
        *[
            ValueEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-60),
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
            recorded_at=_at(-47),
            trial_id=s.t_retry,
            key="pred",
            step=0,
            value="best",
        ),
        *[
            ValueEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-40),
                trial_id=s.t_retry2,
                key="series",
                step=step,
                value=float(step),
            )
            for step in range(SERIES_STEPS)
        ],
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
            recorded_at=_at(-2000),
            trial_id=s.t_root,
            key="note",
            value="hello",
        ),
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1500),
            artifact_id=s.art_json,
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
            artifact_id=s.art_raw,
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
            artifact_id=s.art_missing,
            trial_id=s.t_root,
            key="log",
            filename="run.log",
            content_type="text/plain",
            size_bytes=3,
            sha256=hashlib.sha256(b"xyz").hexdigest(),
            source="system",
        ),
    ]


@pytest.fixture
def scenario(tmp_path):
    store = Store(tmp_path / "client.sqlite")
    artifacts_root = tmp_path / "artifacts"
    app = create_app(store, artifacts_root=artifacts_root)
    ingest = IngestService(store, artifacts_root=artifacts_root)
    s = Scenario(
        store=store,
        app=app,
        ingest=ingest,
        transport=CountingTransport(SyncASGITransport(app=app)),
        sweep_a=sweep_id_for(PROJECT, "alpha"),
        sweep_b=sweep_id_for(PROJECT, "beta"),
        t_root=uuid.uuid4(),
        t_retry=uuid.uuid4(),
        t_retry2=uuid.uuid4(),
        u_trial=uuid.uuid4(),
        ex_root=uuid.uuid4(),
        ex_retry=uuid.uuid4(),
        ex_retry2=uuid.uuid4(),
        ex_u=uuid.uuid4(),
        art_json=uuid.uuid4(),
        art_raw=uuid.uuid4(),
        art_missing=uuid.uuid4(),
    )
    s.apply(_seed_events(s))
    with httpx.Client(
        transport=SyncASGITransport(app=app), base_url="http://t"
    ) as uploader:
        for artifact_id, body in ((s.art_json, JSON_BODY), (s.art_raw, RAW_BODY)):
            response = uploader.put(f"/artifact/{artifact_id}", content=body)
            assert response.status_code == 200
    yield s
    store.close()


class TestClientLifecycle:
    def test_projects_lists_seeded_project(self, scenario):
        with scenario.client() as client:
            assert client.projects() == [PROJECT]

    def test_context_manager_closes(self, scenario):
        with scenario.client() as client:
            client.projects()
        assert client._http.is_closed

    def test_scheme_less_base_url_is_rejected_with_actionable_error(self):
        with pytest.raises(TrackingClientError, match="http://"):
            TrackingClient("atlas.example:443")


class TestFromEnv:
    def test_unset_server_is_a_clear_error(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRACKING_SERVER", raising=False)
        with pytest.raises(TrackingClientError, match="JERNERICS_TRACKING_SERVER"):
            TrackingClient.from_env()

    def test_scheme_less_env_url_is_a_scheme_error(self, monkeypatch):
        monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "atlas.example:443")
        with pytest.raises(TrackingClientError, match="scheme"):
            TrackingClient.from_env()

    def test_scheme_less_env_url_names_env_var_and_pyproject_key(self, monkeypatch):
        monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "atlas.example:443")
        with pytest.raises(TrackingClientError) as excinfo:
            TrackingClient.from_env()
        message = str(excinfo.value)
        assert "atlas.example:443" in message
        assert "JERNERICS_TRACKING_SERVER" in message
        assert "[tool.jernerics] tracking_server" in message

    def test_valid_env_builds_client(self, monkeypatch):
        monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "http://host:8000")
        monkeypatch.setenv("JERNERICS_API_KEY", "secret")
        with TrackingClient.from_env() as client:
            assert client.project("p").name == "p"


class TestRecordsRoundTrip:
    def test_every_handle_method_returns_service_records(self, scenario):
        service = QueryService(scenario.store)
        everything = Selection(project=PROJECT)
        with scenario.client() as client:
            handle = client.project(PROJECT)
            assert handle.sweeps() == service.sweeps(everything)[0]
            assert handle.trials() == service.trials(everything)[0]
            assert handle.lineage() == service.lineage(everything)
            assert handle.executions() == service.executions(everything)
            assert handle.params() == service.trial_params(everything)[0]
            assert handle.value_catalog() == service.value_catalog(everything)
            assert handle.values() == service.values(everything)[0]
            assert handle.artifacts() == service.artifacts(everything)[0]
            assert handle.provenance() == service.provenance(everything)

    def test_records_are_frozen_schema_models(self, scenario):
        from jernerics_schema import (
            ArtifactRecord,
            SweepRecord,
            ValueRecord,
        )

        with scenario.client() as client:
            handle = client.project(PROJECT)
            sweeps = handle.sweeps()
            assert all(isinstance(r, SweepRecord) for r in sweeps)
            with pytest.raises(ValidationError):
                sweeps[0].name = "renamed"
            values = handle.values()
            assert all(isinstance(r, ValueRecord) for r in values)
            artifacts = handle.artifacts()
            assert all(isinstance(r, ArtifactRecord) for r in artifacts)


class TestSelections:
    def test_multi_sweep_union_and_per_sweep(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            both = handle.for_sweeps(scenario.sweep_a, scenario.sweep_b)
            assert sorted(r.name for r in handle.sweeps(both)) == [
                "alpha",
                "beta",
            ]
            assert len(handle.trials(both)) == 4
            alpha = handle.for_sweeps(scenario.sweep_a)
            assert [r.name for r in handle.sweeps(alpha)] == ["alpha"]
            assert len(handle.trials(alpha)) == 3
            beta = handle.for_sweeps(str(scenario.sweep_b))
            assert [r.name for r in handle.sweeps(beta)] == ["beta"]
            assert len(handle.trials(beta)) == 1

    def test_retry_family_spans_all_generations(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            family = handle.for_retry_roots(scenario.t_root)
            assert sorted(t.number for t in handle.trials(family)) == [0, 1, 2]
            lineage = sorted(handle.lineage(family), key=lambda r: r.retry_index)
            assert [r.retry_index for r in lineage] == [0, 1, 2]
            assert lineage[0].trial_id == scenario.t_root
            assert lineage[2].trial_id == scenario.t_retry2
            by_trial = handle.for_trials(scenario.t_retry)
            assert [t.trial_id for t in handle.trials(by_trial)] == [scenario.t_retry]
            by_execution = handle.for_executions(scenario.ex_retry2)
            assert [t.trial_id for t in handle.trials(by_execution)] == [
                scenario.t_retry2
            ]
            assert len(handle.values(family)) == 3 + 4 + SERIES_STEPS

    def test_mismatched_project_selection_is_rejected(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            with pytest.raises(TrackingClientError, match="does not match"):
                handle.sweeps(Selection(project="other"))


class TestFilters:
    def test_params_kind_filter(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            manual = handle.params(kinds=("manual",))
            assert [(r.kind, r.key, r.value) for r in manual] == [
                ("manual", "note", "hello")
            ]
            sampled = handle.params(kinds=("sampled",))
            assert sorted(r.key for r in sampled) == ["lr", "lr"]

    def test_values_filters(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            json_only = handle.values(json_only=True)
            assert {r.key for r in json_only} == {"beta-only", "pred", "snapshot"}
            single = handle.values(keys=("final_loss",))
            assert [(r.key, r.value) for r in single] == [("final_loss", 0.25)]
            states = handle.sweeps(states=("completed",))
            assert [r.name for r in states] == ["beta"]

    def test_artifact_filters(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            missing = handle.artifacts(received=False)
            assert [r.artifact_id for r in missing] == [scenario.art_missing]
            system = handle.artifacts(source="system")
            assert [r.artifact_id for r in system] == [scenario.art_missing]
            received = handle.artifacts(received=True)
            assert {r.artifact_id for r in received} == {
                scenario.art_json,
                scenario.art_raw,
            }


class TestPaginationAndRetry:
    def test_small_pages_return_everything(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            series = handle.values(keys=("series",), page_size=2)
            assert [r.step for r in series] == list(range(SERIES_STEPS))

    def test_iter_values_follows_tokens_lazily(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            before = scenario.transport.requests
            stream = handle.iter_values(keys=("series",), page_size=2)
            head = [next(stream), next(stream)]
            assert [r.step for r in head] == [0, 1]
            assert scenario.transport.requests == before + 1
            rest = [r.step for r in stream]
            assert rest == list(range(2, SERIES_STEPS))
            assert scenario.transport.requests == before + 4

    def test_transient_500_is_retried(self, scenario):
        flaky = FlakyTransport(SyncASGITransport(app=scenario.app), failures=1)
        with TrackingClient("http://t", transport=flaky) as client:
            handle = client.project(PROJECT)
            series = handle.values(keys=("series",), page_size=3)
            assert len(series) == SERIES_STEPS
        assert flaky.failed == 1

    def test_invalid_page_size_rejected_client_side(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            with pytest.raises(TrackingClientError, match="page_size"):
                handle.values(page_size=0)
            with pytest.raises(TrackingClientError, match="page_size"):
                handle.values(page_size=1001)


class TestLatestValuesAndReduce:
    def test_latest_values_picks_last_step_per_key(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            latest = handle.latest_values()
            assert latest["loss"].step == 2
            assert latest["loss"].value == pytest.approx(1.0)
            assert latest["loss"].trial_id == scenario.t_root
            assert latest["series"].step == SERIES_STEPS - 1
            assert latest["snapshot"].observation == {"curve": [1, 2]}
            assert latest["pred"].value == "best"
            assert latest["final_loss"].value == pytest.approx(0.25)
            alpha = handle.for_sweeps(scenario.sweep_a)
            assert set(handle.latest_values(alpha)) == {
                "loss",
                "series",
                "final_loss",
                "snapshot",
                "pred",
            }
            beta = handle.for_sweeps(scenario.sweep_b)
            assert set(handle.latest_values(beta)) == {"loss", "beta-only"}

    def test_reduce_named_and_filtered(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            assert handle.reduce("loss", fn="sum") == pytest.approx(13.5)
            assert handle.reduce("loss", fn=sum) == pytest.approx(13.5)
            assert handle.reduce("loss", fn="min") == pytest.approx(0.5)
            assert handle.reduce("loss", fn="max") == pytest.approx(7.0)
            assert handle.reduce("loss", fn="mean") == pytest.approx(2.7)
            root_only = handle.reduce(
                "loss",
                fn="sum",
                where=lambda r: r.trial_id == scenario.t_root,
            )
            assert root_only == pytest.approx(6.0)
            family = handle.for_retry_roots(scenario.t_root)
            assert handle.reduce("loss", fn="last", selection=family) == (
                pytest.approx(1.0)
            )

    def test_reduce_errors(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            with pytest.raises(TrackingClientError, match="no numeric values"):
                handle.reduce("absent-key")
            with pytest.raises(TrackingClientError, match="unknown reduction"):
                handle.reduce("loss", fn="median")


class TestArtifacts:
    def test_download_streams_byte_identical_file(self, scenario, tmp_path):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            (record,) = [
                r
                for r in handle.artifacts(keys=("model",))
                if r.artifact_id == scenario.art_raw
            ]
            by_record = tmp_path / "by_record.bin"
            assert client.download(record, by_record) == by_record
            assert by_record.read_bytes() == RAW_BODY
            by_uuid = tmp_path / "by_uuid.bin"
            client.download(scenario.art_raw, by_uuid)
            assert by_uuid.read_bytes() == RAW_BODY
            by_text = tmp_path / "by_text.bin"
            client.download(str(scenario.art_raw), by_text)
            assert by_text.read_bytes() == RAW_BODY

    def test_open_yields_binary_stream(self, scenario):
        with scenario.client() as client, client.open(scenario.art_raw) as stream:
            chunks = list(stream.iter_bytes())
        assert b"".join(chunks) == RAW_BODY

    def test_read_json_parses_json_artifact(self, scenario):
        with scenario.client() as client:
            assert client.read_json(scenario.art_json) == {
                "a": 1,
                "b": [True, None],
            }

    def test_declared_only_artifact_is_a_client_error(self, scenario, tmp_path):
        with scenario.client() as client, pytest.raises(TrackingClientError) as excinfo:
            client.download(scenario.art_missing, tmp_path / "no.bin")
        assert excinfo.value.status_code == 404
        assert "blob not received" in str(excinfo.value)


class TestSelectionTokens:
    def test_encode_decode_round_trip_is_byte_stable(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            selection = handle.for_retry_roots(scenario.t_root)
            token = encode_selection(selection)
            assert encode_selection(selection) == token
            twin = handle.for_retry_roots(str(scenario.t_root))
            assert encode_selection(twin) == token
            decoded = decode_selection(token)
            assert decoded == selection

    def test_decoded_selection_drives_identical_results(self, scenario):
        with scenario.client() as client:
            handle = client.project(PROJECT)
            selection = handle.for_retry_roots(scenario.t_root)
            decoded = decode_selection(encode_selection(selection))
            assert handle.values(decoded, keys=("loss",)) == handle.values(
                selection, keys=("loss",)
            )


class TestRawQuery:
    def test_raw_query_returns_columns_and_rows(self, scenario):
        with scenario.client() as client:
            result = client.raw_query("SELECT 1 + 1 AS two")
            assert result == {"columns": ["two"], "rows": [[2]]}

    def test_raw_query_enforces_requested_limit(self, scenario):
        with (
            scenario.client() as client,
            pytest.raises(TrackingClientError, match="limit"),
        ):
            client.raw_query("SELECT 1 AS n", limit=0)


class TestAuth:
    def test_wrong_api_key_is_a_401_client_error(self, tmp_path):
        store = Store(tmp_path / "auth.sqlite")
        try:
            app = create_app(store, api_key="secret123")
            transport = SyncASGITransport(app=app)
            with (
                TrackingClient(
                    "http://t", api_key="wrong-key", transport=transport
                ) as client,
                pytest.raises(TrackingClientError) as excinfo,
            ):
                client.project("p").sweeps()
            assert excinfo.value.status_code == 401
            assert "401" in str(excinfo.value)
            assert "API key" in str(excinfo.value)
        finally:
            store.close()

    def test_correct_api_key_is_accepted(self, tmp_path):
        store = Store(tmp_path / "auth-ok.sqlite")
        try:
            app = create_app(store, api_key="secret123")
            transport = SyncASGITransport(app=app)
            with TrackingClient(
                "http://t", api_key="secret123", transport=transport
            ) as client:
                assert client.projects() == []
        finally:
            store.close()


class TestNoSqlDiscipline:
    def test_client_source_has_no_sql_or_table_names(self):
        from pathlib import Path

        import jernerics.tracking.client as client_module

        source = Path(client_module.__file__).read_text()
        for fragment in (
            "SELECT ",
            " FROM ",
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "ORDER BY",
            "GROUP BY",
            "tracked_values",
            "execution_progress",
        ):
            assert fragment not in source

    def test_client_imports_without_pandas(self):
        code = (
            "import sys; sys.modules['pandas'] = None; "
            "import jernerics.tracking.client as c; "
            "assert c.TrackingClient"
        )
        subprocess.run([sys.executable, "-c", code], check=True)


class TestIntegrations:
    def test_to_dataframe_without_pandas(self, scenario, monkeypatch):
        from jernerics.tracking.integrations import to_dataframe

        monkeypatch.setitem(sys.modules, "pandas", None)
        with pytest.raises(ImportError, match="pandas is not installed"):
            to_dataframe([])

    def test_reconstruct_study_from_snapshots(self, scenario):
        import optuna
        from optuna.distributions import FloatDistribution
        from optuna.trial import TrialState

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        from jernerics.tracking.integrations import reconstruct_study

        with scenario.client() as client:
            selection = client.project(PROJECT).for_sweeps(scenario.sweep_a)
            study = reconstruct_study(selection, client)
        trials = sorted(study.trials, key=lambda t: t.number)
        assert [t.state for t in trials] == [
            TrialState.RUNNING,
            TrialState.COMPLETE,
            TrialState.FAIL,
        ]
        (running, complete, failed) = trials
        assert running.params == {"lr": 0.1}
        assert complete.value == pytest.approx(0.5)
        assert complete.params == {"lr": 0.05}
        assert complete.distributions["lr"] == FloatDistribution(0.001, 0.1, log=True)
        assert failed.params == {}
