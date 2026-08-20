"""Service- and HTTP-level tests for atomic v3 batched event ingest."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionOutcome,
    ExecutionProgressEvent,
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
)
from jernerics_server.http import MAX_INGEST_BYTES, create_app
from jernerics_server.ingest import (
    IngestConflictError,
    IngestService,
    IngestValidationError,
)
from jernerics_server.store import Store

T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

ALL_TABLES = (
    "sweeps",
    "submissions",
    "submission_jobs",
    "trials",
    "trial_params",
    "executions",
    "execution_progress",
    "tracked_values",
    "artifacts",
    "artifact_blobs",
    "reconciliation_conflicts",
)


def at(offset_s: float) -> datetime:
    return T0 + timedelta(seconds=offset_s)


def ns(moment: datetime) -> int:
    delta = moment - EPOCH
    return (
        delta.days * 86_400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1_000


def eid() -> uuid.UUID:
    return uuid.uuid4()


def request_of(events: list) -> IngestRequest:
    return IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)


def rows(store: Store, sql: str, params: list | None = None) -> list[tuple]:
    _, data = store.query(sql, params or [])
    return data


def snapshot(store: Store) -> dict:
    return {
        table: sorted(map(repr, rows(store, f"SELECT * FROM {table}")))
        for table in ALL_TABLES
    }


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "ingest.sqlite") as tracked:
        yield tracked


@pytest.fixture
def service(store):
    return IngestService(store)


@pytest.fixture
def client(store):
    return TestClient(create_app(store))


def post_events(client: TestClient, request: IngestRequest):
    return client.post(
        "/ingest",
        json={
            "protocol_version": PROTOCOL_VERSION,
            "events": [event.model_dump(mode="json") for event in request.events],
        },
    )


class Ids(SimpleNamespace):
    pass


def _mixed_events() -> tuple[list, Ids]:
    ids = Ids(
        sweep=eid(),
        submission=eid(),
        parent=eid(),
        child=eid(),
        execution=eid(),
        artifact=eid(),
    )
    events = [
        SweepSnapshotEvent(
            event_id=eid(),
            recorded_at=at(0),
            project="proj",
            sweep_id=ids.sweep,
            name="alpha",
            state="running",
        ),
        SubmissionSnapshotEvent(
            event_id=eid(),
            recorded_at=at(1),
            submission_id=ids.submission,
            sweep_id=ids.sweep,
            backend="slurm",
            state=SubmissionState.SUBMITTED,
        ),
        TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(3),
            trial_id=ids.child,
            sweep_id=ids.sweep,
            number=1,
            state=TrialState.WAITING,
            retry_of_trial_id=ids.parent,
            retry_root_trial_id=ids.parent,
            retry_index=1,
            params=FlatContext({"lr": 0.2}),
        ),
        TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(2),
            trial_id=ids.parent,
            sweep_id=ids.sweep,
            number=0,
            state=TrialState.WAITING,
            retry_root_trial_id=ids.parent,
            params=FlatContext({"lr": 0.1, "seed": 7}),
        ),
        ExecutionStartEvent(
            event_id=eid(),
            recorded_at=at(4),
            execution_id=ids.execution,
            trial_id=ids.child,
            hostname="node01",
            started_at=at(4),
        ),
        ExecutionHeartbeatEvent(
            event_id=eid(),
            recorded_at=at(5),
            execution_id=ids.execution,
            at=at(5),
        ),
        ExecutionProgressEvent(
            event_id=eid(),
            recorded_at=at(6),
            execution_id=ids.execution,
            current=5,
            total=10,
            unit="epoch",
        ),
        ValueEvent(
            event_id=eid(),
            recorded_at=at(7),
            trial_id=ids.child,
            key="loss",
            step=3,
            value=0.42,
        ),
        ManualParamEvent(
            event_id=eid(),
            recorded_at=at(8),
            trial_id=ids.parent,
            key="note",
            value="hot fix",
        ),
        ExecutionEndEvent(
            event_id=eid(),
            recorded_at=at(9),
            execution_id=ids.execution,
            ended_at=at(9),
            outcome=ExecutionOutcome.SUCCESS,
            exit_code=0,
        ),
        ArtifactDeclarationEvent(
            event_id=eid(),
            recorded_at=at(10),
            artifact_id=ids.artifact,
            trial_id=ids.child,
            execution_id=ids.execution,
            key="model",
            filename="model.pt",
            content_type="application/octet-stream",
            size_bytes=1024,
            sha256="a" * 64,
        ),
    ]
    return events, ids


def _seeded_execution() -> tuple[list, Ids]:
    """A minimal running execution for heartbeat/progress replay tests."""
    ids = Ids(sweep=eid(), trial=eid(), execution=eid())
    events = [
        SweepSnapshotEvent(
            event_id=eid(),
            recorded_at=at(0),
            project="proj",
            sweep_id=ids.sweep,
            name="beta",
            state="running",
        ),
        TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(1),
            trial_id=ids.trial,
            sweep_id=ids.sweep,
            number=0,
            state=TrialState.RUNNING,
            retry_root_trial_id=ids.trial,
        ),
        ExecutionStartEvent(
            event_id=eid(),
            recorded_at=at(2),
            execution_id=ids.execution,
            trial_id=ids.trial,
            hostname="node01",
            started_at=at(2),
        ),
        ExecutionHeartbeatEvent(
            event_id=eid(),
            recorded_at=at(5),
            execution_id=ids.execution,
            at=at(5),
        ),
        ExecutionProgressEvent(
            event_id=eid(),
            recorded_at=at(6),
            execution_id=ids.execution,
            current=5,
            total=10,
            unit="epoch",
        ),
    ]
    return events, ids


class TestIngestService:
    def test_mixed_batch_materializes_every_table(self, store, service):
        events, ids = _mixed_events()
        result = service.apply(request_of(events))
        assert (result.applied, result.duplicates, result.conflicts) == (11, 0, ())

        assert rows(store, "SELECT * FROM sweeps") == [
            (str(ids.sweep), "proj", "alpha", "running", ns(at(0)), ns(at(0)))
        ]
        assert rows(store, "SELECT * FROM submissions") == [
            (
                str(ids.submission),
                str(ids.sweep),
                "slurm",
                "submitted",
                ns(at(1)),
                ns(at(1)),
            )
        ]
        assert rows(
            store,
            "SELECT trial_id, number, state, retry_of_trial_id,"
            " retry_root_trial_id, retry_index FROM trials ORDER BY number",
        ) == [
            (str(ids.parent), 0, "waiting", None, str(ids.parent), 0),
            (str(ids.child), 1, "waiting", str(ids.parent), str(ids.parent), 1),
        ]
        assert sorted(
            rows(store, "SELECT kind, key, value_json FROM trial_params")
        ) == [
            ("manual", "note", '"hot fix"'),
            ("sampled", "lr", "0.1"),
            ("sampled", "lr", "0.2"),
            ("sampled", "seed", "7"),
        ]
        assert rows(
            store,
            "SELECT trial_id, hostname, started_ns, ended_ns, outcome,"
            " exit_code, last_heartbeat_ns, last_observation_ns"
            " FROM executions",
        ) == [
            (
                str(ids.child),
                "node01",
                ns(at(4)),
                ns(at(9)),
                "success",
                0,
                ns(at(5)),
                ns(at(7)),
            )
        ]
        assert rows(store, "SELECT * FROM execution_progress") == [
            (str(ids.execution), 5, 10, "epoch", ns(at(6)))
        ]
        assert rows(
            store,
            "SELECT execution_id, key, step, value_type, scalar_val, text_val,"
            " context, recorded_ns FROM tracked_values",
        ) == [(str(ids.execution), "loss", 3, "scalar", 0.42, None, "{}", ns(at(7)))]
        assert rows(
            store, "SELECT trial_id, received_ns IS NULL, declared_ns FROM artifacts"
        ) == [(str(ids.child), 1, ns(at(10)))]
        assert rows(store, "SELECT count(*) FROM reconciliation_conflicts") == [(0,)]

    def test_identical_replay_is_all_duplicates(self, store, service):
        events, _ = _mixed_events()
        first = service.apply(request_of(events))
        assert (first.applied, first.duplicates) == (11, 0)
        before = snapshot(store)
        second = service.apply(request_of(events))
        assert (second.applied, second.duplicates, second.conflicts) == (0, 11, ())
        assert snapshot(store) == before

    def test_one_conflicting_event_rolls_back_whole_batch(self, store, service):
        events, ids = _mixed_events()
        service.apply(request_of(events))
        before = snapshot(store)
        heartbeat = ExecutionHeartbeatEvent(
            event_id=eid(),
            recorded_at=at(20),
            execution_id=ids.execution,
            at=at(20),
        )
        conflicting = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(21),
            trial_id=ids.parent,
            sweep_id=ids.sweep,
            number=0,
            state=TrialState.RUNNING,
            retry_root_trial_id=ids.parent,
            params=FlatContext({"lr": 0.9}),
        )
        with pytest.raises(IngestConflictError) as excinfo:
            service.apply(request_of([heartbeat, conflicting]))
        assert excinfo.value.event_index == 1
        assert excinfo.value.event_id == conflicting.event_id
        assert "write-once" in excinfo.value.detail
        assert snapshot(store) == before

    def test_stale_heartbeat_and_progress_are_duplicates(self, store, service):
        events, ids = _seeded_execution()
        service.apply(request_of(events))
        stale_heartbeat = ExecutionHeartbeatEvent(
            event_id=eid(),
            recorded_at=at(4),
            execution_id=ids.execution,
            at=at(3),
        )
        result = service.apply(request_of([stale_heartbeat]))
        assert (result.applied, result.duplicates) == (0, 1)
        assert rows(store, "SELECT last_heartbeat_ns FROM executions") == [(ns(at(5)),)]

        stale_progress = ExecutionProgressEvent(
            event_id=eid(),
            recorded_at=at(4),
            execution_id=ids.execution,
            current=3,
            total=10,
            unit="epoch",
        )
        equal_progress = ExecutionProgressEvent(
            event_id=eid(),
            recorded_at=at(6),
            execution_id=ids.execution,
            current=9,
            total=10,
            unit="epoch",
        )
        result = service.apply(request_of([stale_progress, equal_progress]))
        assert (result.applied, result.duplicates) == (0, 2)
        assert rows(store, "SELECT * FROM execution_progress") == [
            (str(ids.execution), 5, 10, "epoch", ns(at(6)))
        ]

        newer_progress = ExecutionProgressEvent(
            event_id=eid(),
            recorded_at=at(7),
            execution_id=ids.execution,
            current=7,
            total=10,
            unit="epoch",
        )
        result = service.apply(request_of([newer_progress]))
        assert (result.applied, result.duplicates) == (1, 0)
        assert rows(store, "SELECT current, updated_ns FROM execution_progress") == [
            (7, ns(at(7)))
        ]
        assert rows(store, "SELECT count(*) FROM reconciliation_conflicts") == [(0,)]

    def test_terminal_execution_end_is_immutable(self, store, service):
        events, ids = _mixed_events()
        service.apply(request_of(events))
        conflicting_end = ExecutionEndEvent(
            event_id=eid(),
            recorded_at=at(11),
            execution_id=ids.execution,
            ended_at=at(11),
            outcome=ExecutionOutcome.FAILURE,
            exit_code=1,
            failure_kind=FailureKind.EXCEPTION,
            failure_summary="boom",
        )
        identical_end = ExecutionEndEvent(
            event_id=eid(),
            recorded_at=at(9),
            execution_id=ids.execution,
            ended_at=at(9),
            outcome=ExecutionOutcome.SUCCESS,
            exit_code=0,
        )
        with pytest.raises(IngestConflictError, match="terminal"):
            service.apply(request_of([conflicting_end]))
        assert rows(store, "SELECT outcome FROM executions") == [("success",)]
        result = service.apply(request_of([identical_end]))
        assert (result.applied, result.duplicates) == (0, 1)

    def test_optimizer_terminal_conflict_recorded_not_applied(self, store, service):
        ids = Ids(sweep=eid(), trial=eid())
        seed = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=ids.sweep,
                name="gamma",
                state="running",
            ),
            TrialSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                trial_id=ids.trial,
                sweep_id=ids.sweep,
                number=0,
                state=TrialState.COMPLETED,
                retry_root_trial_id=ids.trial,
            ),
        ]
        service.apply(request_of(seed))
        conflicting = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(2),
            trial_id=ids.trial,
            sweep_id=ids.sweep,
            number=0,
            state=TrialState.FAILED,
            retry_root_trial_id=ids.trial,
        )
        result = service.apply(request_of([conflicting]))
        assert result.applied == 1
        assert result.duplicates == 0
        assert len(result.conflicts) == 1
        record = result.conflicts[0]
        assert record.trial_id == ids.trial
        assert record.kind == "optimizer_terminal_state"
        assert record.detail == '{"existing":"completed","incoming":"failed"}'
        assert rows(store, "SELECT kind, detail FROM reconciliation_conflicts") == [
            ("optimizer_terminal_state", record.detail)
        ]
        assert rows(store, "SELECT state FROM trials") == [("completed",)]

        replay = service.apply(request_of([conflicting]))
        assert (replay.applied, replay.duplicates, replay.conflicts) == (0, 1, ())
        assert rows(store, "SELECT count(*) FROM reconciliation_conflicts") == [(1,)]

    def test_terminal_to_nonterminal_snapshot_also_conflicts(self, store, service):
        ids = Ids(sweep=eid(), trial=eid())
        service.apply(
            request_of(
                [
                    SweepSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(0),
                        project="proj",
                        sweep_id=ids.sweep,
                        name="delta",
                        state="running",
                    ),
                    TrialSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(1),
                        trial_id=ids.trial,
                        sweep_id=ids.sweep,
                        number=0,
                        state=TrialState.FAILED,
                        retry_root_trial_id=ids.trial,
                    ),
                ]
            )
        )
        result = service.apply(
            request_of(
                [
                    TrialSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(2),
                        trial_id=ids.trial,
                        sweep_id=ids.sweep,
                        number=0,
                        state=TrialState.RUNNING,
                        retry_root_trial_id=ids.trial,
                    )
                ]
            )
        )
        assert len(result.conflicts) == 1
        assert result.conflicts[0].kind == "optimizer_terminal_state"
        assert rows(store, "SELECT state FROM trials") == [("failed",)]

    def test_retry_lineage_is_immutable(self, store, service):
        events, ids = _mixed_events()
        service.apply(request_of(events))
        before = snapshot(store)
        wrong_lineage = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(21),
            trial_id=ids.child,
            sweep_id=ids.sweep,
            number=1,
            state=TrialState.RUNNING,
            retry_of_trial_id=ids.parent,
            retry_root_trial_id=ids.parent,
            retry_index=2,
        )
        with pytest.raises(IngestConflictError, match="lineage"):
            service.apply(request_of([wrong_lineage]))
        wrong_root = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(22),
            trial_id=ids.child,
            sweep_id=ids.sweep,
            number=1,
            state=TrialState.RUNNING,
            retry_of_trial_id=ids.parent,
            retry_root_trial_id=ids.child,
            retry_index=1,
        )
        with pytest.raises(IngestConflictError, match="lineage"):
            service.apply(request_of([wrong_root]))
        assert snapshot(store) == before

    def test_value_lands_on_active_execution(self, store, service):
        ids = Ids(sweep=eid(), trial=eid(), first=eid(), second=eid())
        events = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=ids.sweep,
                name="epsilon",
                state="running",
            ),
            TrialSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                trial_id=ids.trial,
                sweep_id=ids.sweep,
                number=0,
                state=TrialState.RUNNING,
                retry_root_trial_id=ids.trial,
            ),
            ExecutionStartEvent(
                event_id=eid(),
                recorded_at=at(2),
                execution_id=ids.first,
                trial_id=ids.trial,
                hostname="node01",
                started_at=at(2),
            ),
            ExecutionEndEvent(
                event_id=eid(),
                recorded_at=at(3),
                execution_id=ids.first,
                ended_at=at(3),
                outcome=ExecutionOutcome.FAILURE,
                exit_code=1,
                failure_kind=FailureKind.NODE_FAILURE,
            ),
            ExecutionStartEvent(
                event_id=eid(),
                recorded_at=at(4),
                execution_id=ids.second,
                trial_id=ids.trial,
                hostname="node02",
                started_at=at(4),
            ),
            ValueEvent(
                event_id=eid(),
                recorded_at=at(5),
                trial_id=ids.trial,
                key="loss",
                step=0,
                value=0.9,
            ),
        ]
        result = service.apply(request_of(events))
        assert result.applied == 6
        assert rows(store, "SELECT execution_id FROM tracked_values") == [
            (str(ids.second),)
        ]

    def test_value_for_trial_without_execution_conflicts(self, store, service):
        ids = Ids(sweep=eid(), trial=eid())
        orphan_value = ValueEvent(
            event_id=eid(),
            recorded_at=at(5),
            trial_id=ids.trial,
            key="loss",
            step=0,
            value=0.9,
        )
        with pytest.raises(IngestConflictError, match="no execution"):
            service.apply(request_of([orphan_value]))

    def test_rollback_on_unknown_parent_reference(self, store, service):
        sweep = SweepSnapshotEvent(
            event_id=eid(),
            recorded_at=at(0),
            project="proj",
            sweep_id=eid(),
            name="zeta",
            state="running",
        )
        orphan_trial = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(1),
            trial_id=eid(),
            sweep_id=eid(),
            number=0,
            state=TrialState.WAITING,
            retry_root_trial_id=eid(),
        )
        with pytest.raises(IngestValidationError, match="unknown sweep"):
            service.apply(request_of([sweep, orphan_trial]))
        assert snapshot(store).get("sweeps") == []
        assert snapshot(store).get("trials") == []


class TestIngestHTTP:
    def test_mixed_batch_roundtrip(self, store, client):
        events, _ = _mixed_events()
        response = post_events(client, request_of(events))
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 11
        assert body["duplicates"] == 0
        assert body["conflicts"] == []
        replay = post_events(client, request_of(events))
        assert replay.status_code == 200
        assert (replay.json()["accepted"], replay.json()["duplicates"]) == (0, 11)

    def test_rows_visible_from_fresh_readonly_connection(self, store, client):
        events, _ = _mixed_events()
        assert post_events(client, request_of(events)).status_code == 200
        assert rows(store, "SELECT count(*) FROM trials") == [(2,)]
        assert rows(store, "SELECT count(*) FROM tracked_values") == [(1,)]

    def test_conflict_maps_to_409_with_structured_error(self, store, client):
        events, ids = _mixed_events()
        service = IngestService(store)
        service.apply(request_of(events))
        conflicting = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(21),
            trial_id=ids.parent,
            sweep_id=ids.sweep,
            number=0,
            state=TrialState.RUNNING,
            retry_root_trial_id=ids.parent,
            params=FlatContext({"lr": 0.9}),
        )
        response = post_events(client, request_of([conflicting]))
        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "conflict"
        assert body["event_index"] == 0
        assert body["event_id"] == str(conflicting.event_id)
        assert "write-once" in body["detail"]
        assert rows(
            store,
            "SELECT value_json FROM trial_params WHERE key = 'lr' "
            "AND value_json = '0.1'",
        ) == [("0.1",)]

    def test_optimizer_conflict_returns_200_with_conflicts(self, store, client):
        ids = Ids(sweep=eid(), trial=eid())
        seed = request_of(
            [
                SweepSnapshotEvent(
                    event_id=eid(),
                    recorded_at=at(0),
                    project="proj",
                    sweep_id=ids.sweep,
                    name="eta",
                    state="running",
                ),
                TrialSnapshotEvent(
                    event_id=eid(),
                    recorded_at=at(1),
                    trial_id=ids.trial,
                    sweep_id=ids.sweep,
                    number=0,
                    state=TrialState.COMPLETED,
                    retry_root_trial_id=ids.trial,
                ),
            ]
        )
        assert post_events(client, seed).status_code == 200
        conflicting = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(2),
            trial_id=ids.trial,
            sweep_id=ids.sweep,
            number=0,
            state=TrialState.FAILED,
            retry_root_trial_id=ids.trial,
        )
        response = post_events(client, request_of([conflicting]))
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] == 1
        assert body["conflicts"] == [
            {
                "trial_id": str(ids.trial),
                "kind": "optimizer_terminal_state",
                "detail": '{"existing":"completed","incoming":"failed"}',
            }
        ]
        assert rows(store, "SELECT state FROM trials") == [("completed",)]

    def test_value_without_execution_maps_to_409(self, client):
        orphan_value = ValueEvent(
            event_id=eid(),
            recorded_at=at(5),
            trial_id=eid(),
            key="loss",
            step=0,
            value=0.9,
        )
        response = post_events(client, request_of([orphan_value]))
        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "conflict"
        assert body["event_index"] == 0
        assert "no execution" in body["detail"]


class TestIngestLimits:
    def test_wrong_protocol_version_is_422(self, client):
        response = client.post("/ingest", json={"protocol_version": 2, "events": []})
        assert response.status_code == 422

    def test_event_count_over_bound_is_422(self, client):
        heartbeat = {
            "tag": "execution_heartbeat",
            "event_id": str(eid()),
            "recorded_at": at(0).isoformat(),
            "execution_id": str(eid()),
            "at": at(0).isoformat(),
        }
        response = client.post(
            "/ingest",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "events": [heartbeat] * 101,
            },
        )
        assert response.status_code == 422

    def test_content_length_over_cap_is_413(self, client):
        response = client.post(
            "/ingest",
            content=b"x" * (MAX_INGEST_BYTES + 1),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error"] == "payload_too_large"


class TestIngestAuth:
    def test_ingest_requires_bearer_token(self, tmp_path):
        with Store(tmp_path / "auth.sqlite") as tracked:
            app = create_app(tracked, api_key="secret123")
            auth_client = TestClient(app)
            payload = {"protocol_version": PROTOCOL_VERSION, "events": []}
            assert auth_client.post("/ingest", json=payload).status_code == 401
            authorized = auth_client.post(
                "/ingest",
                json=payload,
                headers={"Authorization": "Bearer secret123"},
            )
            assert authorized.status_code == 200
            assert authorized.json() == {
                "accepted": 0,
                "duplicates": 0,
                "conflicts": [],
            }
