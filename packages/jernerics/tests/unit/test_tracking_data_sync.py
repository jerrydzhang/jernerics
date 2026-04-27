from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
from jernerics.tracking.data_sync import (
    ReplayResult,
    _replay_file,
    discover_pb_files,
    replay_tracking,
)
from jernerics.tracking.wire import TrackingWriter
from jernerics_proto import (
    Envelope,
    MetricEvent,
    ParamEvent,
    TrialEndEvent,
    Value,
)


class FakeRpcError(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.UNAVAILABLE

    def details(self):
        return "test error"


def _param_envelope(seq: int, key: str, value: float) -> Envelope:
    return Envelope(
        project="p",
        study_name="s",
        trial_id=0,
        timestamp_ns=1000 + seq,
        seq=seq,
        param=ParamEvent(key=key, value=Value(float_val=value)),
    )


def _metric_envelope(seq: int, key: str, value: float, step: int) -> Envelope:
    return Envelope(
        project="p",
        study_name="s",
        trial_id=0,
        timestamp_ns=1000 + seq,
        seq=seq,
        metric=MetricEvent(key=key, value=value, step=step),
    )


def _trial_end_envelope(seq: int) -> Envelope:
    return Envelope(
        project="p",
        study_name="s",
        trial_id=0,
        timestamp_ns=1000 + seq,
        seq=seq,
        trial_end=TrialEndEvent(),
    )


def _write_events(path: Path, events: list[Envelope]) -> None:
    with TrackingWriter(path) as writer:
        for event in events:
            writer.write_envelope(event)


class TestDiscoverPbFiles:
    def test_finds_all_studies(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a").mkdir(parents=True)
        (tracking / "study_b").mkdir(parents=True)
        (tracking / "study_a" / "0.pb").touch()
        (tracking / "study_a" / "1.pb").touch()
        (tracking / "study_b" / "0.pb").touch()

        result = discover_pb_files(tracking)

        names = [p.name for p in result]
        assert names == ["0.pb", "1.pb", "0.pb"]

    def test_scopes_to_single_study(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a").mkdir(parents=True)
        (tracking / "study_b").mkdir(parents=True)
        (tracking / "study_a" / "0.pb").touch()
        (tracking / "study_b" / "0.pb").touch()

        result = discover_pb_files(tracking, study="study_b")

        assert len(result) == 1
        assert result[0].parent.name == "study_b"

    def test_returns_empty_for_no_files(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        tracking.mkdir()

        result = discover_pb_files(tracking)

        assert result == []

    def test_ignores_non_pb_files(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a").mkdir(parents=True)
        (tracking / "study_a" / "0.pb").touch()
        (tracking / "study_a" / "0.db").touch()

        result = discover_pb_files(tracking)

        assert len(result) == 1


class TestReplayFile:
    def test_sends_all_events(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        pb_file = tmp_path / "0.pb"
        events = [
            _param_envelope(0, "lr", 0.01),
            _metric_envelope(1, "loss", 0.5, 10),
            _trial_end_envelope(2),
        ]
        _write_events(pb_file, events)

        result = _replay_file(pb_file, mock_stub, max_retries=3)

        assert result.events_sent == 3
        assert result.events_total == 3
        assert result.error is None
        assert mock_stub.SendEvent.call_count == 3

    def test_retries_on_failure(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        call_count = 0

        def send_side_effect(_event):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise FakeRpcError()

        mock_stub.SendEvent.side_effect = send_side_effect

        pb_file = tmp_path / "0.pb"
        _write_events(pb_file, [_param_envelope(0, "lr", 0.01)])

        with patch("jernerics.tracking.data_sync._RETRY_BASE_INTERVAL", 0.01):
            result = _replay_file(pb_file, mock_stub, max_retries=5)

        assert result.events_sent == 1
        assert result.error is None

    def test_records_error_on_max_retries_exceeded(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        mock_stub.SendEvent.side_effect = FakeRpcError()

        pb_file = tmp_path / "0.pb"
        _write_events(
            pb_file,
            [_param_envelope(0, "lr", 0.01), _metric_envelope(1, "loss", 0.5, 10)],
        )

        with patch("jernerics.tracking.data_sync._RETRY_BASE_INTERVAL", 0.01):
            result = _replay_file(pb_file, mock_stub, max_retries=2)

        assert result.events_sent == 0
        assert result.error is not None
        assert result.events_total == 2

    def test_handles_corrupt_file(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        pb_file = tmp_path / "0.pb"
        pb_file.write_bytes(b"\xff\xff\xff")

        result = _replay_file(pb_file, mock_stub, max_retries=3)

        assert result.error is not None


class TestReplayTracking:
    def test_replays_all_files(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        tracking = tmp_path / "tracking"
        study_dir = tracking / "study_a"
        study_dir.mkdir(parents=True)

        _write_events(study_dir / "0.pb", [_param_envelope(0, "lr", 0.01)])
        _write_events(study_dir / "1.pb", [_metric_envelope(0, "loss", 0.5, 10)])

        result = replay_tracking(tracking, mock_stub, max_workers=2, max_retries=3)

        assert result.files_processed == 2
        assert result.events_sent == 2
        assert result.errors == []

    def test_scopes_to_study(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        tracking = tmp_path / "tracking"
        (tracking / "study_a").mkdir(parents=True)
        (tracking / "study_b").mkdir(parents=True)
        _write_events(tracking / "study_a" / "0.pb", [_param_envelope(0, "x", 1.0)])
        _write_events(tracking / "study_b" / "0.pb", [_param_envelope(0, "y", 2.0)])

        result = replay_tracking(
            tracking, mock_stub, study="study_b", max_workers=2, max_retries=3
        )

        assert result.files_processed == 1
        assert result.events_sent == 1

    def test_returns_empty_result_for_no_files(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        tracking = tmp_path / "tracking"
        tracking.mkdir()

        result = replay_tracking(tracking, mock_stub, max_retries=3)

        assert result == ReplayResult()

    def test_records_partial_failure(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        study_dir = tracking / "study_a"
        study_dir.mkdir(parents=True)

        _write_events(study_dir / "0.pb", [_param_envelope(0, "lr", 0.01)])

        corrupt_file = study_dir / "1.pb"
        corrupt_file.write_bytes(b"\xff\xff\xff")

        mock_stub = MagicMock()

        result = replay_tracking(tracking, mock_stub, max_workers=2, max_retries=3)

        assert result.files_processed == 2
        assert len(result.errors) == 1
        assert result.events_sent == 1
