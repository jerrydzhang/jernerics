import os
import time
import uuid
from pathlib import Path
from uuid import UUID

from jernerics.tracking.jsonl_io import TrackingReader
from jernerics.tracking.trial_environment import TrialEnvironment
from jernerics_schema import (
    ExecutionEndEvent,
    ExecutionHeartbeatEvent,
    ExecutionOutcome,
    ExecutionStartEvent,
    TrackingEvent,
)
from pydantic import TypeAdapter

_ADAPTER = TypeAdapter(TrackingEvent)


def read_events(path: Path) -> list[TrackingEvent]:
    with TrackingReader(path) as reader:
        return list(reader)


class TestExecutionLifecycle:
    def test_success_path_emits_start_then_end(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=0)

        env.start()
        assert env.tracker is not None
        env.tracker.log_value("loss", 0.5)
        env.finish_execution(ExecutionOutcome.SUCCESS)

        events = read_events(tmp_path / "events" / "0.jsonl")
        start = events[0]
        end = events[-1]
        assert isinstance(start, ExecutionStartEvent)
        assert isinstance(end, ExecutionEndEvent)

        assert start.trial_id == env.trial_id
        assert start.execution_id == env.execution_id
        assert start.hostname == os.uname().nodename
        assert start.started_at.tzinfo is not None

        assert end.execution_id == env.execution_id
        assert end.outcome == ExecutionOutcome.SUCCESS
        assert end.exit_code is None
        assert end.failure_summary is None

    def test_failure_path_emits_failure_with_bounded_summary(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=0)

        env.start()
        env.finish_execution(
            ExecutionOutcome.FAILURE,
            failure_summary=repr(ValueError("boom")),
        )

        end = read_events(tmp_path / "events" / "0.jsonl")[-1]
        assert isinstance(end, ExecutionEndEvent)
        assert end.outcome == ExecutionOutcome.FAILURE
        assert end.failure_summary is not None
        assert "boom" in end.failure_summary

    def test_huge_failure_summary_is_truncated_to_2000(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=0)

        env.start()
        env.finish_execution(ExecutionOutcome.FAILURE, failure_summary="x" * 5000)

        end = read_events(tmp_path / "events" / "0.jsonl")[-1]
        assert isinstance(end, ExecutionEndEvent)
        assert end.failure_summary is not None
        assert len(end.failure_summary) == 2000

    def test_exit_without_finish_leaves_no_terminal_evidence(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=0)

        with env:
            assert env.tracker is not None
            env.tracker.log_value("loss", 0.5)

        events = read_events(tmp_path / "events" / "0.jsonl")
        assert not any(isinstance(event, ExecutionEndEvent) for event in events)

    def test_finish_execution_twice_emits_end_once(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=0)

        env.start()
        env.finish_execution(ExecutionOutcome.SUCCESS)
        env.finish_execution(ExecutionOutcome.FAILURE)

        events = read_events(tmp_path / "events" / "0.jsonl")
        ends = [event for event in events if isinstance(event, ExecutionEndEvent)]
        assert len(ends) == 1
        assert ends[0].outcome == ExecutionOutcome.SUCCESS

    def test_ids_are_uuids_and_stable_within_the_run(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=0)

        with env:
            assert isinstance(env.trial_id, UUID)
            assert isinstance(env.execution_id, UUID)
            trial_id, execution_id = env.trial_id, env.execution_id

        events = read_events(tmp_path / "events" / "0.jsonl")
        assert all(
            getattr(event, "trial_id", None) in (None, trial_id)
            or not hasattr(event, "trial_id")
            for event in events
        )
        assert all(
            event.execution_id == execution_id
            for event in events
            if isinstance(event, (ExecutionStartEvent, ExecutionEndEvent))
        )

    def test_explicit_trial_id_is_honored(self, tmp_path) -> None:
        trial_id = uuid.uuid4()
        env = TrialEnvironment(
            tracking_dir=str(tmp_path), trial_number=3, trial_id=trial_id
        )

        with env:
            pass

        assert env.trial_id == trial_id
        start = read_events(tmp_path / "events" / "3.jsonl")[0]
        assert isinstance(start, ExecutionStartEvent)
        assert start.trial_id == trial_id

    def test_empty_tracking_dir_is_a_passthrough(self) -> None:
        env = TrialEnvironment(tracking_dir="", trial_number=0)

        with env:
            assert env.tracker is None
            assert env.trial_id is None


class TestHeartbeat:
    def test_heartbeats_emitted_independently_of_user_calls(self, tmp_path) -> None:
        env = TrialEnvironment(
            tracking_dir=str(tmp_path), trial_number=0, heartbeat_interval_s=0.05
        )

        with env:
            time.sleep(0.5)  # no user tracking calls at all

        events = read_events(tmp_path / "events" / "0.jsonl")
        heartbeats = [e for e in events if isinstance(e, ExecutionHeartbeatEvent)]
        assert len(heartbeats) >= 3
        assert all(h.execution_id == env.execution_id for h in heartbeats)

    def test_local_heartbeat_file_mtime_advances(self, tmp_path) -> None:
        env = TrialEnvironment(
            tracking_dir=str(tmp_path), trial_number=0, heartbeat_interval_s=0.05
        )
        hb_path = tmp_path / "heartbeats" / "0.heartbeat"

        with env:
            assert hb_path.exists()
            before = hb_path.stat().st_mtime_ns
            time.sleep(0.3)
            after = hb_path.stat().st_mtime_ns

        assert after > before


class TestShipping:
    def test_no_server_addr_starts_no_shipper(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=0)

        with env:
            assert env._sync_client is None

    def test_events_file_is_named_by_trial_number(self, tmp_path) -> None:
        env = TrialEnvironment(tracking_dir=str(tmp_path), trial_number=7)

        with env:
            pass

        assert (tmp_path / "events" / "7.jsonl").exists()


class TestEventValidity:
    def test_every_emitted_event_parses_through_adapter(self, tmp_path) -> None:
        env = TrialEnvironment(
            tracking_dir=str(tmp_path), trial_number=0, heartbeat_interval_s=0.05
        )

        with env:
            assert env.tracker is not None
            env.tracker.log_param("lr", 0.1)
            env.tracker.log_value("loss", 0.5)
            env.tracker.set_progress(1, 2, "epochs")
            time.sleep(0.2)

        for line in (tmp_path / "events" / "0.jsonl").read_text().splitlines():
            parsed = _ADAPTER.validate_json(line)
            assert parsed.tag in {
                "execution_start",
                "manual_param",
                "value",
                "execution_progress",
                "execution_heartbeat",
                "execution_end",
            }
