import hashlib
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from jernerics.tracking.artifact_manifest import ArtifactManifest
from jernerics.tracking.jsonl_io import TrackingReader
from jernerics.tracking.tracker import JsonlTracker, NullTracker
from jernerics_schema import (
    JSON_VALUE_MAX_BYTES,
    ExecutionOutcome,
    FlatContext,
    TrackingEvent,
    ValueEvent,
)
from pydantic import TypeAdapter, ValidationError

_ADAPTER = TypeAdapter(TrackingEvent)


def make_tracker(tmp_path: Path, name: str = "0.jsonl", execution_id=None):
    return JsonlTracker(
        tmp_path / name,
        uuid4(),
        execution_id if execution_id is not None else uuid4(),
        manifest=ArtifactManifest(tmp_path / "0.manifest"),
    )


def read_events(path: Path) -> list[TrackingEvent]:
    with TrackingReader(path) as reader:
        return list(reader)


class TestLogParam:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0.25, 0.25),
            (3, 3),
            ("mlp", "mlp"),
            (True, True),
            (None, None),
        ],
    )
    def test_scalar_variants(self, tmp_path, value, expected) -> None:
        tracker = make_tracker(tmp_path)

        event = tracker.log_param("model", value)

        assert event.value == expected
        assert event.key == "model"
        assert event.trial_id == tracker.trial_id
        assert read_events(tmp_path / "0.jsonl") == [event]

    def test_nested_value_raises_validation_error(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        with pytest.raises(ValidationError):
            tracker.log_param("bad", [1, 2, 3])

    def test_ids_are_unique(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        first = tracker.log_param("a", 1)
        second = tracker.log_param("a", 1)

        assert first.event_id != second.event_id


class TestLogValue:
    def test_step_auto_increments_per_key_from_zero(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        tracker.log_value("loss", 0.5)
        tracker.log_value("loss", 0.4)
        tracker.log_value("acc", 0.9)

        events = read_events(tmp_path / "0.jsonl")
        assert all(isinstance(e, ValueEvent) for e in events)
        assert [e.step for e in events if isinstance(e, ValueEvent)] == [0, 1, 0]

    def test_explicit_step_honored_and_autos_continue_after_it(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        tracker.log_value("loss", 0.5, step=7)
        tracker.log_value("loss", 0.4)

        events = read_events(tmp_path / "0.jsonl")
        assert all(isinstance(e, ValueEvent) for e in events)
        assert [e.step for e in events if isinstance(e, ValueEvent)] == [7, 8]

    def test_scalar_kinds_round_trip(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        tracker.log_value("f", 1.5)
        tracker.log_value("i", 3)
        tracker.log_value("b", True)
        tracker.log_value("s", "text")

        events = read_events(tmp_path / "0.jsonl")
        assert all(isinstance(e, ValueEvent) for e in events)
        assert [e.value for e in events if isinstance(e, ValueEvent)] == [
            1.5,
            3,
            True,
            "text",
        ]

    def test_flat_context_round_trips(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        tracker.log_value("loss", 0.5, context={"seed": 3})

        [event] = read_events(tmp_path / "0.jsonl")
        assert isinstance(event, ValueEvent)
        assert event.context == FlatContext({"seed": 3})

    def test_nested_context_raises_validation_error(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        with pytest.raises(ValidationError):
            tracker.log_value("loss", 0.5, context={"a": {"b": 1}})

    def test_none_value_rejected_by_exactly_one_rule(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        with pytest.raises(ValidationError):
            tracker.log_value("loss", None)

    def test_non_finite_value_raises(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        with pytest.raises(ValueError, match="non-finite"):
            tracker.log_value("loss", float("nan"))
        with pytest.raises(ValueError, match="non-finite"):
            tracker.log_value("loss", float("inf"))

    def test_stamps_tracker_execution_id(self, tmp_path) -> None:
        execution_id = uuid4()
        tracker = make_tracker(tmp_path, execution_id=execution_id)

        event = tracker.log_value("loss", 0.5)

        assert event.execution_id == execution_id
        assert read_events(tmp_path / "0.jsonl") == [event]

    def test_without_execution_id_stays_none(self, tmp_path) -> None:
        tracker = JsonlTracker(tmp_path / "0.jsonl", uuid4())

        event = tracker.log_value("loss", 0.5)

        assert event.execution_id is None


class TestLogJson:
    def test_observation_round_trips(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        tracker.log_json("results", {"loss": 0.5, "ok": True}, step=2)

        [event] = read_events(tmp_path / "0.jsonl")
        assert isinstance(event, ValueEvent)
        assert event.observation == {"loss": 0.5, "ok": True}
        assert event.value is None
        assert event.step == 2

    def test_observation_at_size_boundary_passes(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        blob = {"pad": "x" * (JSON_VALUE_MAX_BYTES - 12)}

        tracker.log_json("results", blob)

        [event] = read_events(tmp_path / "0.jsonl")
        assert isinstance(event, ValueEvent)
        assert event.observation == blob

    def test_oversize_observation_raises(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        blob = {"pad": "x" * (JSON_VALUE_MAX_BYTES + 1)}

        with pytest.raises(ValidationError, match="exceeding"):
            tracker.log_json("results", blob)

    def test_step_counter_shared_with_log_value(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        tracker.log_value("loss", 0.5)
        tracker.log_json("loss", {"epoch": 1})
        tracker.log_value("loss", 0.4)

        events = read_events(tmp_path / "0.jsonl")
        assert all(isinstance(e, ValueEvent) for e in events)
        assert [e.step for e in events if isinstance(e, ValueEvent)] == [0, 1, 2]

    def test_nested_context_raises(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        with pytest.raises(ValidationError):
            tracker.log_json("results", {"a": 1}, context={"meta": [1]})

    def test_stamps_tracker_execution_id(self, tmp_path) -> None:
        execution_id = uuid4()
        tracker = make_tracker(tmp_path, execution_id=execution_id)

        event = tracker.log_json("results", {"loss": 0.5})

        assert event.execution_id == execution_id
        assert read_events(tmp_path / "0.jsonl") == [event]


class TestSetProgress:
    def test_serializes_with_execution_id(self, tmp_path) -> None:
        execution_id = uuid4()
        tracker = make_tracker(tmp_path, execution_id=execution_id)

        event = tracker.set_progress(4, 10, "epochs")

        assert event.execution_id == execution_id
        assert (event.current, event.total, event.unit) == (4, 10, "epochs")
        assert read_events(tmp_path / "0.jsonl") == [event]

    def test_repeated_calls_emit_new_events(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        first = tracker.set_progress(1, 10, "epochs")
        second = tracker.set_progress(2, 10, "epochs")

        events = read_events(tmp_path / "0.jsonl")
        assert events == [first, second]
        assert first.event_id != second.event_id

    def test_requires_execution_id(self, tmp_path) -> None:
        tracker = JsonlTracker(tmp_path / "0.jsonl", uuid4())

        with pytest.raises(RuntimeError, match="execution_id"):
            tracker.set_progress(1, 10, "epochs")


class TestHeartbeat:
    def test_emits_with_default_now(self, tmp_path) -> None:
        execution_id = uuid4()
        tracker = make_tracker(tmp_path, execution_id=execution_id)
        before = datetime.now(timezone.utc)

        event = tracker.emit_heartbeat()

        assert event.execution_id == execution_id
        assert before <= event.at

    def test_explicit_at_honored(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        at = datetime(2026, 1, 1, tzinfo=timezone.utc)

        event = tracker.emit_heartbeat(at)

        assert event.at == at


class TestExecutionLifecycleEvents:
    def test_execution_start_defaults_to_socket_hostname(self, tmp_path) -> None:
        execution_id = uuid4()
        tracker = make_tracker(tmp_path, execution_id=execution_id)

        event = tracker.emit_execution_start()

        assert event.hostname == socket.gethostname()
        assert event.execution_id == execution_id
        assert event.trial_id == tracker.trial_id
        assert event.started_at.tzinfo is not None

    def test_execution_end_carries_outcome_fields(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)

        event = tracker.emit_execution_end(
            ExecutionOutcome.FAILURE,
            exit_code=1,
            failure_summary="boom",
        )

        assert event.outcome == ExecutionOutcome.FAILURE
        assert event.exit_code == 1
        assert event.failure_summary == "boom"


class TestLogArtifact:
    def test_declares_size_sha256_and_content_type(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "model.json"
        payload = b'{"weights": [1, 2, 3]}'
        artifact.write_bytes(payload)

        event = tracker.log_artifact("model", str(artifact))

        assert event.filename == "model.json"
        assert event.content_type == "application/json"
        assert event.size_bytes == len(payload)
        assert event.sha256 == hashlib.sha256(payload).hexdigest()
        assert event.trial_id == tracker.trial_id
        assert event.execution_id == tracker.execution_id

    def test_unknown_extension_falls_back_to_octet_stream(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "weights.bin"
        artifact.write_bytes(b"\x00\x01")

        event = tracker.log_artifact("weights", str(artifact))

        assert event.content_type == "application/octet-stream"

    def test_streams_large_file_sha256(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "big.bin"
        payload = bytes(range(256)) * (1024 * 64)
        artifact.write_bytes(payload)

        event = tracker.log_artifact("big", str(artifact))

        assert event.size_bytes == len(payload)
        assert event.sha256 == hashlib.sha256(payload).hexdigest()

    def test_appends_manifest_entry_with_same_artifact_id(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "model.json"
        artifact.write_bytes(b"{}")

        event = tracker.log_artifact("model", str(artifact))

        manifest = tmp_path / "0.manifest"
        entry = json.loads(manifest.read_text().strip())
        assert entry == {
            "artifact_id": event.artifact_id.hex,
            "key": "model",
            "path": str(artifact),
        }

    def test_system_source_and_content_type_override(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "trial-0.stdout"
        artifact.write_bytes(b"out")

        event = tracker.log_artifact(
            "stdout",
            str(artifact),
            source="system",
            content_type="text/plain",
        )

        assert event.source == "system"
        assert event.content_type == "text/plain"

    def test_context_flows_into_declaration(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "model.json"
        artifact.write_bytes(b"{}")

        event = tracker.log_artifact("model", str(artifact), context={"stage": "final"})

        assert event.context is not None
        assert event.context.root == {"stage": "final"}

    def test_nested_context_raises(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "model.json"
        artifact.write_bytes(b"{}")

        with pytest.raises(ValidationError):
            tracker.log_artifact("model", str(artifact), context={"a": {"b": 1}})


class TestRoundTripThroughAdapter:
    def test_every_emitted_event_parses_back_and_equals_the_model(
        self, tmp_path
    ) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "model.bin"
        artifact.write_bytes(b"payload")

        emitted = [
            tracker.emit_execution_start("host"),
            tracker.log_param("lr", 0.1),
            tracker.log_value("loss", 0.5),
            tracker.log_json("results", {"a": 1}),
            tracker.set_progress(1, 2, "epochs"),
            tracker.emit_heartbeat(),
            tracker.log_artifact("model", str(artifact)),
            tracker.emit_execution_end(ExecutionOutcome.SUCCESS),
        ]
        tracker.close()

        lines = (tmp_path / "0.jsonl").read_text().splitlines()
        assert len(lines) == len(emitted)
        for line, source in zip(lines, emitted, strict=True):
            parsed = _ADAPTER.validate_json(line)
            assert parsed == source

    def test_event_and_artifact_ids_are_uuids(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        artifact = tmp_path / "m.bin"
        artifact.write_bytes(b"x")

        value_event = tracker.log_value("loss", 0.5)
        artifact_event = tracker.log_artifact("m", str(artifact))

        assert isinstance(value_event.event_id, UUID)
        assert isinstance(artifact_event.artifact_id, UUID)
        assert value_event.event_id != artifact_event.artifact_id


class TestClose:
    def test_close_closes_the_writer(self, tmp_path) -> None:
        tracker = make_tracker(tmp_path)
        tracker.log_value("loss", 0.5)
        tracker.close()

        with pytest.raises(ValueError):
            tracker.log_value("loss", 0.4)

    def test_context_manager_closes_on_exit(self, tmp_path) -> None:
        with make_tracker(tmp_path) as tracker:
            tracker.log_value("loss", 0.5)

        assert tracker.writer.file.closed


class TestNullTracker:
    def test_all_surface_methods_are_no_ops(self, tmp_path) -> None:
        tracker = NullTracker()

        with tracker:
            tracker.log_param("a", 1)
            tracker.log_value("b", 2)
            tracker.log_json("c", {"d": 1})
            tracker.set_progress(1, 2, "epochs")
            tracker.emit_heartbeat()
            tracker.emit_execution_start()
            tracker.emit_execution_end(ExecutionOutcome.SUCCESS)
            tracker.log_artifact("m", str(tmp_path))
        tracker.close()
