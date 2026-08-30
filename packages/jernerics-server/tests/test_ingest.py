"""Service- and HTTP-level tests for atomic v3 batched event ingest."""

import subprocess
import sys
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
    JobResourceEvent,
    JobSnapshotEvent,
    ManualParamEvent,
    SubmissionSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrackingEvent,
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
    "job_resources",
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
                None,
                None,
                None,
                None,
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

    def test_stamped_value_after_retry_attributes_to_named_execution(
        self, store, service
    ):
        ids = Ids(sweep=eid(), trial=eid(), first=eid(), second=eid())
        setup = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=ids.sweep,
                name="theta",
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
        ]
        # The retry (E2) is active and has already logged "loss" steps
        # 0-2 as unstamped live events.
        live = [
            ValueEvent(
                event_id=eid(),
                recorded_at=at(10 + step),
                trial_id=ids.trial,
                key="loss",
                step=step,
                value=0.5 + step,
            )
            for step in range(3)
        ]
        service.apply(request_of(setup + live))
        # E1's buffered values replay afterwards, stamped with their
        # execution: same trial, same key, the same steps.
        replayed = [
            ValueEvent(
                event_id=eid(),
                recorded_at=at(2.1 + step * 0.1),
                trial_id=ids.trial,
                execution_id=ids.first,
                key="loss",
                step=step,
                value=0.1 + step,
            )
            for step in range(3)
        ]

        result = service.apply(request_of(replayed))

        assert result.applied == 3
        assert rows(
            store,
            "SELECT step, scalar_val FROM tracked_values "
            "WHERE execution_id = ? ORDER BY step",
            [str(ids.first)],
        ) == [(0, 0.1), (1, 0.1 + 1), (2, 0.1 + 2)]
        assert rows(
            store,
            "SELECT step, scalar_val FROM tracked_values "
            "WHERE execution_id = ? ORDER BY step",
            [str(ids.second)],
        ) == [(0, 0.5), (1, 0.5 + 1), (2, 0.5 + 2)]
        follow_up = ValueEvent(
            event_id=eid(),
            recorded_at=at(20),
            trial_id=ids.trial,
            execution_id=ids.first,
            key="loss",
            step=3,
            value=0.4,
        )
        assert service.apply(request_of([follow_up])).applied == 1

    def test_stamped_value_unknown_execution_is_validation_error(self, store, service):
        stamped = ValueEvent(
            event_id=eid(),
            recorded_at=at(5),
            trial_id=eid(),
            execution_id=eid(),
            key="loss",
            step=0,
            value=0.9,
        )
        with pytest.raises(IngestValidationError, match="unknown execution"):
            service.apply(request_of([stamped]))

    def test_stamped_value_for_foreign_trial_execution_conflicts(self, store, service):
        ids = Ids(sweep=eid(), owner=eid(), other=eid(), execution=eid())
        service.apply(
            request_of(
                [
                    SweepSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(0),
                        project="proj",
                        sweep_id=ids.sweep,
                        name="iota",
                        state="running",
                    ),
                    TrialSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(1),
                        trial_id=ids.owner,
                        sweep_id=ids.sweep,
                        number=0,
                        state=TrialState.RUNNING,
                        retry_root_trial_id=ids.owner,
                    ),
                    ExecutionStartEvent(
                        event_id=eid(),
                        recorded_at=at(2),
                        execution_id=ids.execution,
                        trial_id=ids.owner,
                        hostname="node01",
                        started_at=at(2),
                    ),
                ]
            )
        )
        mismatched = ValueEvent(
            event_id=eid(),
            recorded_at=at(5),
            trial_id=ids.other,
            execution_id=ids.execution,
            key="loss",
            step=0,
            value=0.9,
        )
        with pytest.raises(IngestConflictError, match="owned by trial"):
            service.apply(request_of([mismatched]))

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

    def test_stamped_value_replay_after_retry_returns_200(self, store, client):
        ids = Ids(sweep=eid(), trial=eid(), first=eid(), second=eid())
        seed = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=ids.sweep,
                name="kappa",
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
                value=0.5,
            ),
        ]
        assert post_events(client, request_of(seed)).status_code == 200
        replayed = [
            ValueEvent(
                event_id=eid(),
                recorded_at=at(2.5),
                trial_id=ids.trial,
                execution_id=ids.first,
                key="loss",
                step=step,
                value=0.1 + step,
            )
            for step in range(2)
        ]

        response = post_events(client, request_of(replayed))

        assert response.status_code == 200
        assert response.json()["accepted"] == 2
        assert rows(
            store,
            "SELECT step FROM tracked_values WHERE execution_id = ? ORDER BY step",
            [str(ids.first)],
        ) == [(0,), (1,)]
        assert rows(
            store,
            "SELECT step FROM tracked_values WHERE execution_id = ? ORDER BY step",
            [str(ids.second)],
        ) == [(0,)]
        later = ValueEvent(
            event_id=eid(),
            recorded_at=at(9),
            trial_id=ids.trial,
            key="loss",
            step=1,
            value=0.6,
        )
        assert post_events(client, request_of([later])).status_code == 200

    def test_stamped_value_mismatched_trial_maps_to_409(self, store, client):
        ids = Ids(sweep=eid(), owner=eid(), other=eid(), execution=eid())
        seed = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=ids.sweep,
                name="lambda",
                state="running",
            ),
            TrialSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                trial_id=ids.owner,
                sweep_id=ids.sweep,
                number=0,
                state=TrialState.RUNNING,
                retry_root_trial_id=ids.owner,
            ),
            ExecutionStartEvent(
                event_id=eid(),
                recorded_at=at(2),
                execution_id=ids.execution,
                trial_id=ids.owner,
                hostname="node01",
                started_at=at(2),
            ),
        ]
        assert post_events(client, request_of(seed)).status_code == 200
        mismatched = ValueEvent(
            event_id=eid(),
            recorded_at=at(5),
            trial_id=ids.other,
            execution_id=ids.execution,
            key="loss",
            step=0,
            value=0.9,
        )

        response = post_events(client, request_of([mismatched]))

        assert response.status_code == 409
        body = response.json()
        assert body["error"] == "conflict"
        assert body["event_index"] == 0
        assert body["event_id"] == str(mismatched.event_id)
        assert str(ids.execution) in body["detail"]
        assert str(ids.other) in body["detail"]
        assert str(ids.owner) in body["detail"]
        assert rows(store, "SELECT count(*) FROM tracked_values") == [(0,)]


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


def _submission_seed(ids: Ids) -> list:
    return [
        SweepSnapshotEvent(
            event_id=eid(),
            recorded_at=at(0),
            project="proj",
            sweep_id=ids.sweep,
            name="alpha",
            state="running",
        ),
    ]


class TestSubmissionProvenance:
    def test_provenance_columns_materialize(self, store, service):
        sweep = eid()
        submission = eid()
        events = _submission_seed(Ids(sweep=sweep)) + [
            SubmissionSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                submission_id=submission,
                sweep_id=sweep,
                backend="slurm",
                state=SubmissionState.SUBMITTED,
                submitted_at=at(1),
                expected_trials=12,
                git_hash="abc123",
                config_source="configs/sweep.py",
            ),
        ]
        result = service.apply(request_of(events))
        assert result.applied == 2
        assert rows(
            store,
            "SELECT submitted_ns, expected_trials, git_hash, config_source "
            "FROM submissions",
        ) == [(ns(at(1)), 12, "abc123", "configs/sweep.py")]
        event = SubmissionSnapshotEvent(
            event_id=eid(),
            recorded_at=at(1),
            submission_id=submission,
            sweep_id=sweep,
            backend="slurm",
            state=SubmissionState.SUBMITTED,
            submitted_at=at(1),
            expected_trials=12,
            git_hash="abc123",
            config_source="configs/sweep.py",
        )
        service.apply(request_of(_submission_seed(Ids(sweep=sweep)) + [event]))
        result = service.apply(
            request_of(
                _submission_seed(Ids(sweep=sweep))
                + [
                    SubmissionSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(1),
                        submission_id=submission,
                        sweep_id=sweep,
                        backend="slurm",
                        state=SubmissionState.SUBMITTED,
                        submitted_at=at(1),
                        expected_trials=12,
                        git_hash="abc123",
                        config_source="configs/sweep.py",
                    )
                ]
            )
        )
        assert result.duplicates == 2
        assert rows(store, "SELECT COUNT(*) FROM submissions") == [(1,)]

    def test_differing_provenance_conflicts(self, store, service):
        submission = eid()
        sweep = eid()
        service.apply(
            request_of(
                _submission_seed(Ids(sweep=sweep))
                + [
                    SubmissionSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(1),
                        submission_id=submission,
                        sweep_id=sweep,
                        backend="slurm",
                        state=SubmissionState.SUBMITTED,
                        submitted_at=at(1),
                        expected_trials=12,
                        git_hash="abc123",
                        config_source="configs/sweep.py",
                    )
                ]
            )
        )
        with pytest.raises(IngestConflictError, match="write-once"):
            service.apply(
                request_of(
                    [
                        SubmissionSnapshotEvent(
                            event_id=eid(),
                            recorded_at=at(2),
                            submission_id=submission,
                            sweep_id=sweep,
                            backend="slurm",
                            state=SubmissionState.SUBMITTED,
                            expected_trials=13,
                        )
                    ]
                )
            )

    def test_null_provenance_later_filled_by_replay(self, store, service):
        submission = eid()
        sweep = eid()
        service.apply(
            request_of(
                _submission_seed(Ids(sweep=sweep))
                + [
                    SubmissionSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(1),
                        submission_id=submission,
                        sweep_id=sweep,
                        backend="slurm",
                        state=SubmissionState.SUBMITTED,
                    )
                ]
            )
        )
        service.apply(
            request_of(
                [
                    SubmissionSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(2),
                        submission_id=submission,
                        sweep_id=sweep,
                        backend="slurm",
                        state=SubmissionState.RUNNING,
                        submitted_at=at(1),
                        expected_trials=12,
                    )
                ]
            )
        )
        assert rows(
            store, "SELECT state, submitted_ns, expected_trials FROM submissions"
        ) == [("running", ns(at(1)), 12)]


class TestJobSnapshot:
    def _seeded(self, sweep: uuid.UUID, submission: uuid.UUID) -> list:
        return _submission_seed(Ids(sweep=sweep)) + [
            SubmissionSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                submission_id=submission,
                sweep_id=sweep,
                backend="slurm",
                state=SubmissionState.SUBMITTED,
            ),
        ]

    def test_jobs_upsert_per_scheduler_identity(self, store, service):
        sweep, submission = eid(), eid()
        events = self._seeded(sweep, submission) + [
            JobSnapshotEvent(
                event_id=eid(),
                recorded_at=at(2),
                job_id=eid(),
                submission_id=submission,
                scheduler_job_id="123",
                role="trials",
                state=SubmissionState.SUBMITTED,
            ),
            JobSnapshotEvent(
                event_id=eid(),
                recorded_at=at(2),
                job_id=eid(),
                submission_id=submission,
                scheduler_job_id="124",
                role="checker",
                state=SubmissionState.SUBMITTED,
            ),
        ]
        result = service.apply(request_of(events))
        assert result.applied == 4
        assert rows(
            store,
            "SELECT scheduler_job_id, role, state FROM submission_jobs "
            "ORDER BY scheduler_job_id",
        ) == [("123", "trials", "submitted"), ("124", "checker", "submitted")]

    def test_duplicate_replay_reports_duplicates(self, store, service):
        sweep, submission, job = eid(), eid(), eid()
        events = self._seeded(sweep, submission) + [
            JobSnapshotEvent(
                event_id=eid(),
                recorded_at=at(2),
                job_id=job,
                submission_id=submission,
                scheduler_job_id="123",
                role="trials",
                state=SubmissionState.SUBMITTED,
            ),
        ]
        service.apply(request_of(events))
        replay = service.apply(
            request_of(
                [
                    JobSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(2),
                        job_id=job,
                        submission_id=submission,
                        scheduler_job_id="123",
                        role="trials",
                        state=SubmissionState.SUBMITTED,
                    )
                ]
            )
        )
        assert replay.duplicates == 1
        assert rows(store, "SELECT COUNT(*) FROM submission_jobs") == [(1,)]

    def test_differing_role_conflicts(self, store, service):
        sweep, submission, job = eid(), eid(), eid()
        service.apply(
            request_of(
                self._seeded(sweep, submission)
                + [
                    JobSnapshotEvent(
                        event_id=eid(),
                        recorded_at=at(2),
                        job_id=job,
                        submission_id=submission,
                        scheduler_job_id="123",
                        role="trials",
                        state=SubmissionState.SUBMITTED,
                    )
                ]
            )
        )
        with pytest.raises(IngestConflictError, match="role is immutable"):
            service.apply(
                request_of(
                    [
                        JobSnapshotEvent(
                            event_id=eid(),
                            recorded_at=at(3),
                            job_id=job,
                            submission_id=submission,
                            scheduler_job_id="123",
                            role="checker",
                            state=SubmissionState.SUBMITTED,
                        )
                    ]
                )
            )

    def test_scheduler_identity_unique_per_submission(self, store, service):
        sweep, submission = eid(), eid()
        service.apply(request_of(self._seeded(sweep, submission)))
        with pytest.raises(IngestConflictError, match="already held by job"):
            service.apply(
                request_of(
                    [
                        JobSnapshotEvent(
                            event_id=eid(),
                            recorded_at=at(2),
                            job_id=eid(),
                            submission_id=submission,
                            scheduler_job_id="123",
                            role="trials",
                            state=SubmissionState.SUBMITTED,
                        ),
                        JobSnapshotEvent(
                            event_id=eid(),
                            recorded_at=at(2),
                            job_id=eid(),
                            submission_id=submission,
                            scheduler_job_id="123",
                            role="checker",
                            state=SubmissionState.SUBMITTED,
                        ),
                    ]
                )
            )

    def test_job_for_unknown_submission_is_invalid(self, store, service):
        with pytest.raises(IngestValidationError, match="unknown submission"):
            service.apply(
                request_of(
                    [
                        JobSnapshotEvent(
                            event_id=eid(),
                            recorded_at=at(2),
                            job_id=eid(),
                            submission_id=eid(),
                            scheduler_job_id="123",
                            role="trials",
                            state=SubmissionState.SUBMITTED,
                        )
                    ]
                )
            )

    def test_conflicting_provenance_maps_to_409(self, store, client):
        sweep, submission = eid(), eid()
        seeded = _submission_seed(Ids(sweep=sweep)) + [
            SubmissionSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                submission_id=submission,
                sweep_id=sweep,
                backend="slurm",
                state=SubmissionState.SUBMITTED,
                git_hash="abc",
            ),
        ]
        assert post_events(client, request_of(seeded)).status_code == 200
        conflicting = [
            SubmissionSnapshotEvent(
                event_id=eid(),
                recorded_at=at(2),
                submission_id=submission,
                sweep_id=sweep,
                backend="slurm",
                state=SubmissionState.SUBMITTED,
                git_hash="def",
            ),
        ]
        response = post_events(client, request_of(conflicting))
        assert response.status_code == 409

    def _execution_events(
        self,
        sweep: uuid.UUID,
        trial: uuid.UUID,
        execution: uuid.UUID,
        number: int,
        *,
        heartbeats: int,
    ) -> list:
        events: list[TrackingEvent] = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=sweep,
                name="watched",
                state="running",
            ),
            TrialSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                trial_id=trial,
                sweep_id=sweep,
                number=number,
                state=TrialState.RUNNING,
                retry_root_trial_id=trial,
            ),
            ExecutionStartEvent(
                event_id=eid(),
                recorded_at=at(2),
                execution_id=execution,
                trial_id=trial,
                hostname="node01",
                started_at=at(2),
            ),
        ]
        for beat in range(heartbeats):
            events.append(
                ExecutionHeartbeatEvent(
                    event_id=eid(),
                    recorded_at=at(3 + beat),
                    execution_id=execution,
                    at=at(3 + beat),
                )
            )
        return events

    def test_missing_heartbeats_synthesize_no_failure(self, store, service):
        sweep, trial, execution = eid(), eid(), eid()
        service.apply(
            request_of(self._execution_events(sweep, trial, execution, 0, heartbeats=0))
        )
        assert rows(
            store,
            "SELECT ended_ns, outcome, exit_code, failure_kind, "
            "failure_summary, last_heartbeat_ns FROM executions",
        ) == [(None, None, None, None, None, None)]

    def test_dashboard_inputs_derive_from_heartbeats_without_stale_label(
        self, store, service
    ):
        sweep = eid()
        quiet_trial, quiet_exec = eid(), eid()
        live_trial, live_exec = eid(), eid()
        service.apply(
            request_of(
                self._execution_events(sweep, quiet_trial, quiet_exec, 0, heartbeats=0)
                + self._execution_events(sweep, live_trial, live_exec, 1, heartbeats=2)
            )
        )
        stale_threshold_ns = ns(at(2))
        quiet, live = (
            rows(
                store,
                "SELECT execution_id FROM executions WHERE ended_ns IS NULL "
                "AND (last_heartbeat_ns IS NULL OR last_heartbeat_ns < ?)",
                [stale_threshold_ns],
            ),
            rows(
                store,
                "SELECT execution_id FROM executions WHERE ended_ns IS NULL "
                "AND last_heartbeat_ns >= ?",
                [stale_threshold_ns],
            ),
        )
        assert quiet == [(str(quiet_exec),)]
        assert live == [(str(live_exec),)]

        table_columns = store.query("PRAGMA table_info(executions)")[1]
        assert all("stale" not in row[1] for row in table_columns)
        for table in ALL_TABLES:
            for row in rows(store, f"SELECT * FROM {table}"):
                assert "stale" not in [str(value).lower() for value in row]


class TestJobResource:
    def _event(
        self,
        event_id,
        *,
        recorded_at=None,
        study_name="study",
        submission_id=None,
        wall_time_s=3_723.0,
        cpu_pct=4213.45,
    ):
        return JobResourceEvent(
            event_id=event_id,
            recorded_at=recorded_at if recorded_at is not None else at(2),
            job_id="990001",
            study_name=study_name,
            submission_id=submission_id,
            wall_time_s=wall_time_s,
            cpu_pct=cpu_pct,
        )

    def test_applies_without_linked_entities(self, store, service):
        result = service.apply(
            request_of(
                [self._event(eid(), study_name="never-seen", submission_id="nope")]
            )
        )

        assert result.applied == 1
        assert rows(
            store,
            "SELECT job_id, study_name, submission_id, wall_time_s, cpu_pct "
            "FROM job_resources",
        ) == [("990001", "never-seen", "nope", 3_723.0, 4213.45)]

    def test_recapture_fills_nulls_but_never_overwrites(self, store, service):
        event_id = eid()
        first = service.apply(
            request_of([self._event(event_id, study_name=None, wall_time_s=None)])
        )
        second = service.apply(
            request_of(
                [
                    self._event(
                        event_id,
                        recorded_at=at(3),
                        study_name="study",
                        submission_id="sub-1",
                        wall_time_s=9_999.0,
                    )
                ]
            )
        )
        third = service.apply(
            request_of([self._event(event_id, recorded_at=at(4), study_name="other")])
        )

        assert (first.applied, second.applied, third.applied) == (1, 1, 0)
        assert third.duplicates == 1
        assert rows(
            store,
            "SELECT study_name, submission_id, wall_time_s FROM job_resources",
        ) == [("study", "sub-1", 9_999.0)]

    def test_ships_over_http(self, store, client):
        response = client.post(
            "/ingest",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "events": [self._event(eid()).model_dump(mode="json")],
            },
        )

        assert response.status_code == 200
        assert response.json()["accepted"] == 1
        assert rows(store, "SELECT COUNT(*) FROM job_resources") == [(1,)]


class TestDeterministicSweepIdentity:
    def test_sweep_id_for_is_stable_across_calls_and_processes(self):
        from jernerics_schema import sweep_id_for

        first = sweep_id_for("proj", "alpha")
        assert sweep_id_for("proj", "alpha") == first
        assert sweep_id_for("proj", "beta") != first

        code = (
            "from jernerics_schema import sweep_id_for;"
            "print(sweep_id_for('proj', 'alpha'))"
        )
        other = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert other.stdout.strip() == str(first)

    def test_two_submissions_share_sweep_id_without_conflict(self, store, service):
        from jernerics_schema import sweep_id_for

        sweep = sweep_id_for("proj", "alpha")
        events: list[TrackingEvent] = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=sweep,
                name="alpha",
                state="running",
            ),
        ]
        for submission in (eid(), eid()):
            events.append(
                SubmissionSnapshotEvent(
                    event_id=eid(),
                    recorded_at=at(1),
                    submission_id=submission,
                    sweep_id=sweep,
                    backend="slurm",
                    state=SubmissionState.SUBMITTED,
                )
            )
        result = service.apply(request_of(events))
        assert result.applied == 3
        assert rows(store, "SELECT COUNT(*) FROM sweeps") == [(1,)]
        assert rows(store, "SELECT COUNT(*) FROM submissions") == [(2,)]


class TestOptimizerMirrorColumns:
    """objective/distributions/attrs carried by trial snapshots."""

    def _events(
        self,
        sweep: uuid.UUID,
        trial: uuid.UUID,
        *,
        state: TrialState = TrialState.COMPLETED,
        **overrides,
    ) -> list:
        sweep_event = SweepSnapshotEvent(
            event_id=eid(),
            recorded_at=at(0),
            project="proj",
            sweep_id=sweep,
            name="s",
            state="running",
        )
        trial_event = TrialSnapshotEvent(
            event_id=eid(),
            recorded_at=at(1),
            trial_id=trial,
            sweep_id=sweep,
            number=0,
            retry_root_trial_id=trial,
            state=state,
            params=FlatContext({"lr": 0.1}),
            **overrides,
        )
        return [sweep_event, trial_event]

    def _seed(self, service, sweep, trial, **kwargs):
        events = self._events(sweep, trial, **kwargs)
        return service.apply(request_of(events)), events

    def test_columns_materialize_from_snapshot(self, store, service):
        sweep, trial = eid(), eid()
        distributions = FlatContext(
            {"lr": '{"name": "FloatDistribution", "attributes": {"low": 0.0}}'}
        )
        attrs = FlatContext({"jernerics_trial_id": str(trial), "retry_index": 0})
        result, _ = self._seed(
            service,
            sweep,
            trial,
            objective=0.25,
            distributions=distributions,
            attrs=attrs,
        )

        assert result.applied == 2
        assert rows(
            store,
            "SELECT objective, distributions_json, attrs_json FROM trials",
        ) == [
            (
                0.25,
                '{"lr":"{\\"name\\": \\"FloatDistribution\\", '
                '\\"attributes\\": {\\"low\\": 0.0}}"}',
                f'{{"jernerics_trial_id":"{trial}","retry_index":0}}',
            )
        ]

    def test_identical_replay_is_duplicate(self, store, service):
        sweep, trial = eid(), eid()
        self._seed(service, sweep, trial, objective=0.25, attrs=FlatContext({"g": 1}))

        result, _ = self._seed(
            service, sweep, trial, objective=0.25, attrs=FlatContext({"g": 1})
        )

        assert (result.applied, result.duplicates) == (0, 2)

    def test_objective_filled_from_earlier_running_snapshot(self, store, service):
        sweep, trial = eid(), eid()
        self._seed(service, sweep, trial, state=TrialState.RUNNING)

        result, _ = self._seed(service, sweep, trial, objective=0.25)

        assert result.applied == 1
        assert rows(store, "SELECT objective FROM trials") == [(0.25,)]

    def test_differing_objective_conflicts(self, store, service):
        sweep, trial = eid(), eid()
        self._seed(service, sweep, trial, objective=0.25)

        with pytest.raises(IngestConflictError, match="objective is write-once"):
            self._seed(service, sweep, trial, objective=0.75)

        assert rows(store, "SELECT objective FROM trials") == [(0.25,)]

    def test_differing_objective_maps_to_409(self, store, client, service):
        sweep, trial = eid(), eid()
        self._seed(service, sweep, trial, objective=0.25)

        response = post_events(
            client,
            request_of(self._events(sweep, trial, objective=0.75)),
        )

        assert response.status_code == 409
        assert response.json()["error"] == "conflict"

    def test_differing_distributions_conflict(self, store, service):
        sweep, trial = eid(), eid()
        self._seed(
            service,
            sweep,
            trial,
            distributions=FlatContext({"lr": '{"name": "FloatDistribution"}'}),
        )

        with pytest.raises(IngestConflictError, match="distributions_json"):
            self._seed(
                service,
                sweep,
                trial,
                distributions=FlatContext({"lr": '{"name": "IntDistribution"}'}),
            )

    def test_terminal_state_conflict_leaves_columns_untouched(self, store, service):
        sweep, trial = eid(), eid()
        self._seed(
            service,
            sweep,
            trial,
            objective=0.25,
            attrs=FlatContext({"jernerics_trial_id": str(trial)}),
        )

        result, _ = self._seed(
            service,
            sweep,
            trial,
            state=TrialState.FAILED,
            objective=0.9,
            attrs=FlatContext({"jernerics_trial_id": "other"}),
        )

        assert len(result.conflicts) == 1
        assert result.conflicts[0].kind == "optimizer_terminal_state"
        assert rows(store, "SELECT state, objective, attrs_json FROM trials") == [
            ("completed", 0.25, f'{{"jernerics_trial_id":"{trial}"}}')
        ]
        assert rows(store, "SELECT kind FROM reconciliation_conflicts") == [
            ("optimizer_terminal_state",)
        ]

    def test_null_objective_after_filled_is_noop(self, store, service):
        sweep, trial = eid(), eid()
        self._seed(service, sweep, trial, objective=0.25)

        result, _ = self._seed(service, sweep, trial, state=TrialState.RUNNING)

        assert result.duplicates == 1
        assert rows(store, "SELECT objective FROM trials") == [(0.25,)]


class TestArtifactDeclarationColumns:
    """context/source carried by artifact declarations."""

    def _seed(
        self,
        service,
        sweep,
        trial,
        artifact,
        *,
        source="user",
        context=None,
    ):
        events = [
            SweepSnapshotEvent(
                event_id=eid(),
                recorded_at=at(0),
                project="proj",
                sweep_id=sweep,
                name="alpha",
                state="running",
            ),
            TrialSnapshotEvent(
                event_id=eid(),
                recorded_at=at(1),
                trial_id=trial,
                sweep_id=sweep,
                number=0,
                state=TrialState.RUNNING,
                retry_root_trial_id=trial,
            ),
            ArtifactDeclarationEvent(
                event_id=eid(),
                recorded_at=at(2),
                artifact_id=artifact,
                trial_id=trial,
                key="stdout",
                filename="trial-0.stdout",
                content_type="text/plain",
                size_bytes=3,
                sha256="a" * 64,
                source=source,
                context=context,
            ),
        ]
        return service.apply(request_of(events)), events

    def test_context_and_source_materialize(self, store, service):
        sweep, trial, artifact = eid(), eid(), eid()

        self._seed(
            service,
            sweep,
            trial,
            artifact,
            source="system",
            context=FlatContext({"stage": "final"}),
        )

        assert rows(store, "SELECT context_json, source FROM artifacts") == [
            ('{"stage":"final"}', "system")
        ]

    def test_defaults_are_user_and_null_context(self, store, service):
        sweep, trial, artifact = eid(), eid(), eid()

        self._seed(service, sweep, trial, artifact)

        assert rows(store, "SELECT context_json, source FROM artifacts") == [
            (None, "user")
        ]

    def test_differing_source_conflicts(self, store, service):
        sweep, trial, artifact = eid(), eid(), eid()
        self._seed(service, sweep, trial, artifact, source="system")

        with pytest.raises(IngestConflictError, match="differing facts"):
            self._seed(service, sweep, trial, artifact, source="user")

        assert rows(store, "SELECT source FROM artifacts") == [("system",)]

    def test_identical_replay_is_duplicate(self, store, service):
        sweep, trial, artifact = eid(), eid(), eid()
        self._seed(service, sweep, trial, artifact, context=FlatContext({"a": 1}))

        result, _ = self._seed(
            service, sweep, trial, artifact, context=FlatContext({"a": 1})
        )

        assert (result.applied, result.duplicates) == (0, 3)
