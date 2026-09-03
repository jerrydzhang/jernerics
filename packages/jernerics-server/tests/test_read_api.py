"""Typed domain read API over the v3 store: acceptance tests.

Seeds a real temp Store via IngestService with mixed events (retry chains,
JSON and non-step values, artifacts with and without blobs, two sweeps),
then exercises every domain endpoint plus the hardened /query.
"""

import hashlib
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionOutcome,
    ExecutionStartEvent,
    FlatContext,
    IngestRequest,
    JobResourceEvent,
    ManualParamEvent,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    ValueEvent,
    sweep_id_for,
)
from jernerics_server import http as http_module
from jernerics_server import store as store_module
from jernerics_server.http import MAX_ROWS, create_app
from jernerics_server.ingest import IngestService
from jernerics_server.store import Store

PROJECT = "read-api"
ARTIFACT_BODY = b"artifact-bytes"
ARTIFACT_SHA = hashlib.sha256(ARTIFACT_BODY).hexdigest()

SCAN_STEPS = 30
EXTRA_SCAN_STEPS = 4


def _at(seconds: float) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


@dataclass
class Scenario:
    client: TestClient
    store: Store
    ingest: IngestService
    sweep_a: uuid.UUID
    sweep_b: uuid.UUID
    t_root: uuid.UUID
    t_retry: uuid.UUID
    t_other: uuid.UUID
    u_trial: uuid.UUID
    ex_active: uuid.UUID
    ex_ended: uuid.UUID
    ex_stale: uuid.UUID
    ex_quiet: uuid.UUID
    art_received: uuid.UUID
    art_missing: uuid.UUID

    def apply(self, events: list) -> None:
        self.ingest.apply(
            IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
        )

    def selection(self, **filters: Iterable[uuid.UUID]) -> dict:
        return {
            "project": PROJECT,
            **{
                name: [str(value) for value in values]
                for name, values in filters.items()
            },
        }


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
            state="running",
        ),
        SubmissionSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-7100),
            submission_id=uuid.uuid4(),
            sweep_id=s.sweep_a,
            backend="local",
            state=SubmissionState.SUBMITTED,
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
            retry_root_trial_id=s.t_root,
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-2400),
            trial_id=s.t_retry,
            sweep_id=s.sweep_a,
            number=1,
            state=TrialState.COMPLETED,
            params=FlatContext(root={"lr": 0.2}),
            objective=0.5,
            distributions=FlatContext(root={"lr": "loguniform"}),
            attrs=FlatContext(root={"seed": 1}),
            retry_of_trial_id=s.t_root,
            retry_root_trial_id=s.t_root,
            retry_index=1,
        ),
        TrialSnapshotEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1800),
            trial_id=s.t_other,
            sweep_id=s.sweep_a,
            number=2,
            state=TrialState.FAILED,
            retry_root_trial_id=s.t_other,
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
            recorded_at=_at(-1800),
            execution_id=s.ex_active,
            trial_id=s.t_root,
            hostname="node01",
            started_at=_at(-1800),
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-7200),
            execution_id=s.ex_ended,
            trial_id=s.t_retry,
            hostname="node02",
            started_at=_at(-7200),
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-10800),
            execution_id=s.ex_stale,
            trial_id=s.t_other,
            hostname="node03",
            started_at=_at(-10800),
        ),
        ExecutionStartEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1200),
            execution_id=s.ex_quiet,
            trial_id=s.u_trial,
            hostname="node04",
            started_at=_at(-1200),
        ),
        ExecutionHeartbeatEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-10),
            execution_id=s.ex_active,
            at=_at(-10),
        ),
        ExecutionHeartbeatEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-7200),
            execution_id=s.ex_stale,
            at=_at(-7200),
        ),
        ExecutionHeartbeatEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-300),
            execution_id=s.ex_quiet,
            at=_at(-300),
        ),
        ExecutionEndEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-5400),
            execution_id=s.ex_ended,
            ended_at=_at(-5400),
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
            trial_id=s.t_root,
            key="final_loss",
            step=0,
            value=0.5,
        ),
        *[
            ValueEvent(
                event_id=uuid.uuid4(),
                recorded_at=_at(-40),
                trial_id=s.t_root,
                key="scan",
                step=step,
                value=float(step),
            )
            for step in range(SCAN_STEPS)
        ],
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-5300),
            trial_id=s.t_retry,
            key="loss",
            step=0,
            value=0.5,
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-5300),
            trial_id=s.t_retry,
            key="snapshot",
            step=0,
            observation={"curve": [1, 2]},
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-5300),
            trial_id=s.t_retry,
            key="pred",
            step=0,
            value="best",
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-100),
            trial_id=s.u_trial,
            key="loss",
            step=0,
            value=7.0,
        ),
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-100),
            trial_id=s.u_trial,
            key="b-only",
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
            recorded_at=_at(-5000),
            artifact_id=s.art_received,
            trial_id=s.t_retry,
            execution_id=s.ex_ended,
            key="model",
            filename="model.bin",
            content_type="application/octet-stream",
            size_bytes=len(ARTIFACT_BODY),
            sha256=ARTIFACT_SHA,
        ),
        ArtifactDeclarationEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1500),
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


def _extra_scan_events(s: Scenario) -> list:
    return [
        ValueEvent(
            event_id=uuid.uuid4(),
            recorded_at=_at(-1),
            trial_id=s.t_root,
            key="scan",
            step=step,
            value=float(step),
        )
        for step in range(SCAN_STEPS, SCAN_STEPS + EXTRA_SCAN_STEPS)
    ]


@pytest.fixture
def scenario(tmp_path):
    store = Store(tmp_path / "read.sqlite")
    artifacts_root = tmp_path / "artifacts"
    app = create_app(store, artifacts_root=artifacts_root)
    client = TestClient(app)
    ingest = IngestService(store, artifacts_root=artifacts_root)
    s = Scenario(
        client=client,
        store=store,
        ingest=ingest,
        sweep_a=sweep_id_for(PROJECT, "alpha"),
        sweep_b=sweep_id_for(PROJECT, "beta"),
        t_root=uuid.uuid4(),
        t_retry=uuid.uuid4(),
        t_other=uuid.uuid4(),
        u_trial=uuid.uuid4(),
        ex_active=uuid.uuid4(),
        ex_ended=uuid.uuid4(),
        ex_stale=uuid.uuid4(),
        ex_quiet=uuid.uuid4(),
        art_received=uuid.uuid4(),
        art_missing=uuid.uuid4(),
    )
    s.apply(_seed_events(s))
    response = client.put(f"/artifact/{s.art_received}", content=ARTIFACT_BODY)
    assert response.status_code == 200
    yield s
    store.close()


class TestProjectsAndSweeps:
    def test_projects_lists_the_seeded_project(self, scenario):
        body = scenario.client.post("/projects").json()
        assert body == {"records": [PROJECT], "next_token": None}

    def test_cross_sweep_selection(self, scenario):
        both = {
            "selection": scenario.selection(sweeps=(scenario.sweep_a, scenario.sweep_b))
        }
        body = scenario.client.post("/sweeps", json=both).json()
        assert sorted(r["name"] for r in body["records"]) == ["alpha", "beta"]
        assert body["next_token"] is None
        single = {"selection": scenario.selection(sweeps=(scenario.sweep_a,))}
        body = scenario.client.post("/sweeps", json=single).json()
        assert [r["name"] for r in body["records"]] == ["alpha"]
        body = scenario.client.post("/trials", json=both).json()
        assert len(body["records"]) == 4
        body = scenario.client.post("/trials", json=single).json()
        assert len(body["records"]) == 3

    def test_sweeps_state_filter(self, scenario):
        body = scenario.client.post(
            "/sweeps",
            json={
                "selection": scenario.selection(),
                "states": ["completed"],
            },
        ).json()
        assert body["records"] == []


class TestTrialsAndLineage:
    def test_trials_carry_objective_and_attrs_not_flat_params(self, scenario):
        body = scenario.client.post(
            "/trials",
            json={"selection": scenario.selection(trials=(scenario.t_retry,))},
        ).json()
        (record,) = body["records"]
        assert record["trial_id"] == str(scenario.t_retry)
        assert record["objective"] == pytest.approx(0.5)
        assert record["distributions"] == {"lr": "loguniform"}
        assert record["attrs"] == {"seed": 1}
        assert record["params"] == {}

    def test_trials_state_filter(self, scenario):
        body = scenario.client.post(
            "/trials",
            json={"selection": scenario.selection(), "states": ["completed"]},
        ).json()
        assert [r["trial_id"] for r in body["records"]] == [str(scenario.t_retry)]

    def test_retry_roots_only_returns_first_attempts(self, scenario):
        body = scenario.client.post(
            "/trials",
            json={"selection": scenario.selection(), "retry_roots_only": True},
        ).json()
        assert sorted(r["trial_id"] for r in body["records"]) == sorted(
            str(x) for x in (scenario.t_root, scenario.t_other, scenario.u_trial)
        )

    def test_retry_family_expansion_via_retry_roots(self, scenario):
        by_trial = {
            "selection": scenario.selection(trials=(scenario.t_retry,)),
        }
        body = scenario.client.post("/trials", json=by_trial).json()
        assert [r["trial_id"] for r in body["records"]] == [str(scenario.t_retry)]
        by_root = {"selection": scenario.selection(retry_roots=(scenario.t_root,))}
        body = scenario.client.post("/trials", json=by_root).json()
        assert sorted(r["trial_id"] for r in body["records"]) == sorted(
            str(x) for x in (scenario.t_root, scenario.t_retry)
        )

    def test_lineage_chains_the_family(self, scenario):
        body = scenario.client.post(
            "/lineage",
            json={"selection": scenario.selection(retry_roots=(scenario.t_root,))},
        ).json()
        records = sorted(body["records"], key=lambda r: r["retry_index"])
        (root, retry) = records
        assert root["trial_id"] == str(scenario.t_root)
        assert root["retry_of_trial_id"] is None
        assert root["retry_root_trial_id"] == str(scenario.t_root)
        assert root["retry_index"] == 0
        assert retry["trial_id"] == str(scenario.t_retry)
        assert retry["retry_of_trial_id"] == str(scenario.t_root)
        assert retry["retry_root_trial_id"] == str(scenario.t_root)
        assert retry["retry_index"] == 1
        assert retry["number"] == 1
        assert retry["sweep_id"] == str(scenario.sweep_a)


class TestParams:
    def test_params_by_kind(self, scenario):
        body = scenario.client.post(
            "/trial-params",
            json={
                "selection": scenario.selection(),
                "kinds": ["manual"],
            },
        ).json()
        assert [(r["kind"], r["key"], r["value"]) for r in body["records"]] == [
            ("manual", "note", "hello")
        ]
        body = scenario.client.post(
            "/trial-params",
            json={
                "selection": scenario.selection(),
                "kinds": ["sampled"],
            },
        ).json()
        assert sorted(r["key"] for r in body["records"]) == ["lr", "lr"]

    def test_params_are_paged(self, scenario):
        body = scenario.client.post(
            "/trial-params",
            json={"selection": scenario.selection(), "page": {"limit": 2}},
        ).json()
        assert len(body["records"]) == 2
        assert body["next_token"] is not None
        body = scenario.client.post(
            "/trial-params",
            json={
                "selection": scenario.selection(),
                "page": {"limit": 2},
                "page_token": body["next_token"],
            },
        ).json()
        assert len(body["records"]) == 1
        assert body["next_token"] is None


class TestValueCatalog:
    def test_catalog_lists_kinds_and_counts(self, scenario):
        body = scenario.client.post(
            "/value-catalog", json={"selection": scenario.selection()}
        ).json()
        catalog = {(r["key"], r["kind"]): r for r in body["records"]}
        assert catalog["loss", "scalar"]["n_points"] == 5
        assert catalog["loss", "scalar"]["n_trials"] == 3
        assert catalog["scan", "scalar"]["n_points"] == SCAN_STEPS
        assert catalog["scan", "scalar"]["n_trials"] == 1
        assert catalog["snapshot", "json"]["n_points"] == 1
        assert catalog["pred", "json"]["n_points"] == 1
        assert catalog["b-only", "json"]["n_points"] == 1
        assert catalog["final_loss", "scalar"]["latest_step"] == 0

    def test_missing_key_in_one_sweep_is_absent_not_fatal(self, scenario):
        sweep_a_only = {"selection": scenario.selection(sweeps=(scenario.sweep_a,))}
        body = scenario.client.post("/value-catalog", json=sweep_a_only).json()
        assert "b-only" not in {r["key"] for r in body["records"]}
        both = {
            "selection": scenario.selection(sweeps=(scenario.sweep_a, scenario.sweep_b))
        }
        body = scenario.client.post("/value-catalog", json=both).json()
        assert "b-only" in {r["key"] for r in body["records"]}


class TestValues:
    def test_json_only_returns_observations_and_json_scalars(self, scenario):
        body = scenario.client.post(
            "/values",
            json={"selection": scenario.selection(), "json_only": True},
        ).json()
        by_key = {r["key"]: r for r in body["records"]}
        assert set(by_key) == {"snapshot", "pred", "b-only"}
        assert by_key["snapshot"]["observation"] == {"curve": [1, 2]}
        assert by_key["snapshot"]["value"] is None
        assert by_key["pred"]["value"] == "best"
        assert by_key["pred"]["observation"] is None
        assert by_key["snapshot"]["execution_id"] == str(scenario.ex_ended)
        assert by_key["snapshot"]["trial_id"] == str(scenario.t_retry)

    def test_non_step_scalar_retrievable(self, scenario):
        body = scenario.client.post(
            "/values",
            json={"selection": scenario.selection(), "keys": ["final_loss"]},
        ).json()
        (record,) = body["records"]
        assert record["key"] == "final_loss"
        assert record["step"] == 0
        assert record["value"] == pytest.approx(0.5)

    def test_steps_and_series_filters(self, scenario):
        body = scenario.client.post(
            "/values",
            json={
                "selection": scenario.selection(),
                "key": "loss",
                "steps": [0],
            },
        ).json()
        assert len(body["records"]) == 3
        body = scenario.client.post(
            "/values",
            json={
                "selection": scenario.selection(trials=(scenario.t_root,)),
                "key": "loss",
            },
        ).json()
        assert [r["step"] for r in body["records"]] == [0, 1, 2]


class TestKeysetPagination:
    def _page(self, scenario, token=None):
        body = {"selection": scenario.selection(), "keys": ["scan"]}
        body["page"] = {"limit": 10}
        if token is not None:
            body["page_token"] = token
        response = scenario.client.post("/values", json=body)
        assert response.status_code == 200
        return response.json()

    def test_ingest_completing_between_pages_appears_in_later_pages(self, scenario):
        first = self._page(scenario)
        assert [r["step"] for r in first["records"]] == list(range(10))
        thread = threading.Thread(
            target=scenario.apply, args=(_extra_scan_events(scenario),)
        )
        thread.start()
        thread.join()
        steps = [r["step"] for r in first["records"]]
        token = first["next_token"]
        assert token is not None
        while token is not None:
            page = self._page(scenario, token)
            steps.extend(r["step"] for r in page["records"])
            token = page["next_token"]
        assert steps == list(range(SCAN_STEPS + EXTRA_SCAN_STEPS))

    def test_malformed_token_is_structured_400(self, scenario):
        body = scenario.client.post(
            "/values",
            json={
                "selection": scenario.selection(),
                "keys": ["scan"],
                "page_token": "not-a-token",
            },
        )
        assert body.status_code == 400
        error = body.json()["error"]
        assert error["code"] == "invalid_page_token"
        assert error["detail"]

    def test_token_rejected_under_changed_filters(self, scenario):
        first = self._page(scenario)
        body = scenario.client.post(
            "/values",
            json={
                "selection": scenario.selection(),
                "page": {"limit": 10},
                "page_token": first["next_token"],
            },
        )
        assert body.status_code == 400
        assert body.json()["error"]["code"] == "page_token_mismatch"

    def test_offset_is_rejected(self, scenario):
        body = scenario.client.post(
            "/values",
            json={
                "selection": scenario.selection(),
                "page": {"limit": 10, "offset": 10},
            },
        )
        assert body.status_code == 400
        assert body.json()["error"]["code"] == "offset_unsupported"


class TestMonitoring:
    def _monitoring_by_execution(self, scenario, **extra):
        body = {"selection": scenario.selection(), **extra}
        records = scenario.client.post("/executions", json=body).json()["records"]
        return {r["execution_id"]: r["monitoring"] for r in records}

    def test_labels_derive_from_facts(self, scenario):
        labels = self._monitoring_by_execution(scenario)
        assert labels[str(scenario.ex_active)] == "active"
        assert labels[str(scenario.ex_quiet)] == "quiet"
        assert labels[str(scenario.ex_stale)] == "stale"
        assert labels[str(scenario.ex_ended)] == "ended"

    def test_derive_false_omits_labels(self, scenario):
        labels = self._monitoring_by_execution(scenario, derive=False)
        assert set(labels.values()) == {None}

    def test_threshold_override_per_call(self, scenario):
        labels = self._monitoring_by_execution(scenario, heartbeat_stale_s=3600.0)
        assert labels[str(scenario.ex_quiet)] == "active"
        assert labels[str(scenario.ex_stale)] == "stale"
        assert labels[str(scenario.ex_ended)] == "ended"

    def test_states_filter(self, scenario):
        body = scenario.client.post(
            "/executions",
            json={"selection": scenario.selection(), "states": ["ended"]},
        ).json()
        assert [r["execution_id"] for r in body["records"]] == [str(scenario.ex_ended)]

    def test_selection_by_execution_id(self, scenario):
        body = scenario.client.post(
            "/executions",
            json={"selection": scenario.selection(executions=(scenario.ex_active,))},
        ).json()
        assert [r["execution_id"] for r in body["records"]] == [str(scenario.ex_active)]

    def test_labels_are_never_persisted(self, scenario):
        _, rows = scenario.store.query("PRAGMA table_info(executions)")
        names = {row[1] for row in rows}
        assert "monitoring" not in names
        assert "active" not in names


class TestArtifacts:
    def test_availability_reflects_blob_receipt(self, scenario):
        body = scenario.client.post(
            "/artifacts", json={"selection": scenario.selection()}
        ).json()
        by_id = {r["artifact_id"]: r for r in body["records"]}
        assert by_id[str(scenario.art_received)]["received_ns"] is not None
        assert by_id[str(scenario.art_missing)]["received_ns"] is None

    def test_received_false_returns_declared_only(self, scenario):
        body = scenario.client.post(
            "/artifacts",
            json={"selection": scenario.selection(), "received": False},
        ).json()
        assert [r["artifact_id"] for r in body["records"]] == [
            str(scenario.art_missing)
        ]

    def test_received_true_and_source_filters(self, scenario):
        body = scenario.client.post(
            "/artifacts",
            json={"selection": scenario.selection(), "received": True},
        ).json()
        assert [r["artifact_id"] for r in body["records"]] == [
            str(scenario.art_received)
        ]
        body = scenario.client.post(
            "/artifacts",
            json={"selection": scenario.selection(), "source": "system"},
        ).json()
        assert [r["key"] for r in body["records"]] == ["log"]


class TestProvenance:
    def test_provenance_carries_submission_facts(self, scenario):
        body = scenario.client.post(
            "/provenance", json={"selection": scenario.selection()}
        ).json()
        by_sweep = {r["sweep_id"]: r for r in body["records"]}
        alpha = by_sweep[str(scenario.sweep_a)]
        assert alpha["backend"] == "local"
        assert alpha["git_hash"] == "a" * 40
        assert alpha["config_source"] == "config.py"
        assert alpha["expected_trials"] == 3
        assert isinstance(alpha["submitted_at_ns"], int)
        beta = by_sweep[str(scenario.sweep_b)]
        assert beta["backend"] == "slurm"
        assert beta["git_hash"] is None
        assert beta["submitted_at_ns"] is None

    def test_provenance_respects_sweep_filter(self, scenario):
        body = scenario.client.post(
            "/provenance",
            json={"selection": scenario.selection(sweeps=(scenario.sweep_b,))},
        ).json()
        assert [r["sweep_id"] for r in body["records"]] == [str(scenario.sweep_b)]


class TestJobResources:
    def _seed(self, scenario, *, event_id=None, job_id="771001", study_name="alpha"):
        scenario.apply(
            [
                JobResourceEvent(
                    event_id=event_id if event_id is not None else uuid.uuid4(),
                    recorded_at=_at(-60),
                    job_id=job_id,
                    study_name=study_name,
                    submission_id=None,
                    wall_time_s=3_723.0,
                    cpu_time_s=101_400.0,
                    cpu_pct=4213.45,
                    max_rss_mb=2_560.0,
                    ave_rss_mb=2_457.6,
                    alloc_cpus=8,
                    req_mem="16G",
                    alloc_tres="cpu=8,mem=16G,billing=8",
                    node_list="node[01-02]",
                    state="COMPLETED",
                    exit_code="0:0",
                )
            ]
        )

    def test_scopes_by_study_name_through_the_sweep(self, scenario):
        self._seed(scenario)
        self._seed(
            scenario,
            event_id=uuid.uuid4(),
            job_id="771002",
            study_name="beta",
        )
        self._seed(
            scenario,
            event_id=uuid.uuid4(),
            job_id="771003",
            study_name="not-a-study-here",
        )

        body = scenario.client.post(
            "/job-resources",
            json={"selection": scenario.selection(sweeps=(scenario.sweep_a,))},
        ).json()
        assert [r["job_id"] for r in body["records"]] == ["771001"]
        body = scenario.client.post(
            "/job-resources", json={"selection": scenario.selection()}
        ).json()
        assert [r["job_id"] for r in body["records"]] == ["771001", "771002"]

    def test_named_job_ids_bypass_study_scoping(self, scenario):
        self._seed(
            scenario,
            event_id=uuid.uuid4(),
            job_id="771003",
            study_name="not-a-study-here",
        )

        body = scenario.client.post(
            "/job-resources",
            json={"selection": scenario.selection(), "job_ids": ["771003"]},
        ).json()

        assert [r["job_id"] for r in body["records"]] == ["771003"]

    def test_record_carries_every_captured_field(self, scenario):
        self._seed(scenario)

        body = scenario.client.post(
            "/job-resources", json={"selection": scenario.selection()}
        ).json()

        record = body["records"][0]
        assert record["study_name"] == "alpha"
        assert record["wall_time_s"] == pytest.approx(3_723.0)
        assert record["cpu_time_s"] == pytest.approx(101_400.0)
        assert record["cpu_pct"] == pytest.approx(4213.45)
        assert record["max_rss_mb"] == pytest.approx(2_560.0)
        assert record["ave_rss_mb"] == pytest.approx(2_457.6)
        assert record["alloc_cpus"] == 8
        assert record["req_mem"] == "16G"
        assert record["alloc_tres"] == "cpu=8,mem=16G,billing=8"
        assert record["node_list"] == "node[01-02]"
        assert record["state"] == "COMPLETED"
        assert record["exit_code"] == "0:0"
        assert record["recorded_at"].endswith("Z")

    def test_pages_by_job_id(self, scenario):
        for number in (1, 2, 3):
            self._seed(
                scenario,
                event_id=uuid.uuid4(),
                job_id=f"77100{number}",
            )

        first = scenario.client.post(
            "/job-resources",
            json={"selection": scenario.selection(), "page": {"limit": 2}},
        ).json()
        second = scenario.client.post(
            "/job-resources",
            json={
                "selection": scenario.selection(),
                "page": {"limit": 2},
                "page_token": first["next_token"],
            },
        ).json()

        assert [r["job_id"] for r in first["records"]] == ["771001", "771002"]
        assert [r["job_id"] for r in second["records"]] == ["771003"]
        assert second["next_token"] is None


class TestQueryHardening:
    def test_row_limit_enforced(self, scenario):
        sql = (
            "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt "
            f"LIMIT {MAX_ROWS + 1}) SELECT x FROM cnt"
        )
        body = scenario.client.post("/query", json={"sql": sql})
        assert body.status_code == 400
        assert "error" in body.json()

    def test_long_running_query_aborted_by_guard(self, scenario, monkeypatch):
        monkeypatch.setattr(store_module, "MAX_QUERY_SECONDS", 0.0)
        sql = (
            "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt) "
            "SELECT count(*) FROM cnt"
        )
        body = scenario.client.post("/query", json={"sql": sql})
        assert body.status_code == 400
        error = body.json()["error"]
        assert error["code"] == "query_resource_limit"
        assert "resource limits" in error["detail"]


class TestSharedService:
    def test_http_handlers_contain_no_sql(self):
        source = Path(http_module.__file__).read_text()
        for fragment in (
            " FROM ",
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "GROUP BY",
            "ORDER BY",
        ):
            assert fragment not in source


class TestReadEndpointAuth:
    def test_domain_endpoints_require_bearer(self, tmp_path):
        store = Store(tmp_path / "auth.sqlite")
        try:
            app = create_app(store, api_key="secret123")
            client = TestClient(app)
            request = {"selection": {"project": "p"}}
            assert client.post("/trials", json=request).status_code == 401
            response = client.post(
                "/trials",
                json=request,
                headers={"Authorization": "Bearer secret123"},
            )
            assert response.status_code == 200
        finally:
            store.close()
