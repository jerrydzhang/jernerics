from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
from jernerics.tracking.batch_sync import (
    ReplayResult,
    _replay_file,
    discover_manifest_files,
    discover_pb_files,
    replay_tracking,
    sync_artifacts,
)
from jernerics.tracking.pb_io import TrackingWriter
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


class TestReplayFileWithAuth:
    def test_passes_metadata_to_send_event(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        pb_file = tmp_path / "0.pb"
        _write_events(pb_file, [_param_envelope(0, "lr", 0.01)])

        metadata = [("x-api-key", "secret")]
        _replay_file(pb_file, mock_stub, max_retries=3, metadata=metadata)

        call_kwargs = mock_stub.SendEvent.call_args
        assert call_kwargs.kwargs.get("metadata") == metadata

    def test_no_metadata_when_none(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        pb_file = tmp_path / "0.pb"
        _write_events(pb_file, [_param_envelope(0, "lr", 0.01)])

        _replay_file(pb_file, mock_stub, max_retries=3, metadata=None)

        call_kwargs = mock_stub.SendEvent.call_args
        assert call_kwargs.kwargs.get("metadata") is None


class TestDiscoverPbFiles:
    def test_finds_all_studies(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_b" / "events").mkdir(parents=True)
        (tracking / "study_a" / "events" / "0.pb").touch()
        (tracking / "study_a" / "events" / "1.pb").touch()
        (tracking / "study_b" / "events" / "0.pb").touch()

        result = discover_pb_files(tracking)

        names = [p.name for p in result]
        assert names == ["0.pb", "1.pb", "0.pb"]

    def test_scopes_to_single_study(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_b" / "events").mkdir(parents=True)
        (tracking / "study_a" / "events" / "0.pb").touch()
        (tracking / "study_b" / "events" / "0.pb").touch()

        result = discover_pb_files(tracking, study="study_b")

        assert len(result) == 1
        assert result[0].parent.name == "events"

    def test_returns_empty_for_no_files(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        tracking.mkdir()

        result = discover_pb_files(tracking)

        assert result == []

    def test_ignores_non_pb_files(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_a" / "events" / "0.pb").touch()
        (tracking / "study_a" / "events" / "0.db").touch()

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

        def send_side_effect(_event, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise FakeRpcError()

        mock_stub.SendEvent.side_effect = send_side_effect

        pb_file = tmp_path / "0.pb"
        _write_events(pb_file, [_param_envelope(0, "lr", 0.01)])

        with patch("jernerics.tracking.batch_sync._RETRY_BASE_INTERVAL", 0.01):
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

        with patch("jernerics.tracking.batch_sync._RETRY_BASE_INTERVAL", 0.01):
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
        events_dir = tracking / "study_a" / "events"
        events_dir.mkdir(parents=True)

        _write_events(events_dir / "0.pb", [_param_envelope(0, "lr", 0.01)])
        _write_events(events_dir / "1.pb", [_metric_envelope(0, "loss", 0.5, 10)])

        result = replay_tracking(tracking, mock_stub, max_workers=2, max_retries=3)

        assert result.files_processed == 2
        assert result.events_sent == 2
        assert result.errors == []
        assert not (events_dir / "0.pb").exists()
        assert not (events_dir / "1.pb").exists()

    def test_scopes_to_study(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_b" / "events").mkdir(parents=True)
        _write_events(
            tracking / "study_a" / "events" / "0.pb", [_param_envelope(0, "x", 1.0)]
        )
        _write_events(
            tracking / "study_b" / "events" / "0.pb", [_param_envelope(0, "y", 2.0)]
        )

        result = replay_tracking(
            tracking, mock_stub, study="study_b", max_workers=2, max_retries=3
        )

        assert result.files_processed == 1
        assert result.events_sent == 1
        assert not (tracking / "study_b" / "events" / "0.pb").exists()
        assert (tracking / "study_a" / "events" / "0.pb").exists()

    def test_returns_empty_result_for_no_files(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        tracking = tmp_path / "tracking"
        tracking.mkdir()

        result = replay_tracking(tracking, mock_stub, max_retries=3)

        assert result == ReplayResult()
        assert not result.errors

    def test_records_partial_failure(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        events_dir = tracking / "study_a" / "events"
        events_dir.mkdir(parents=True)

        _write_events(events_dir / "0.pb", [_param_envelope(0, "lr", 0.01)])

        corrupt_file = events_dir / "1.pb"
        corrupt_file.write_bytes(b"\xff\xff\xff")

        mock_stub = MagicMock()

        result = replay_tracking(tracking, mock_stub, max_workers=2, max_retries=3)

        assert result.files_processed == 2
        assert len(result.errors) == 1
        assert result.events_sent == 1
        assert (events_dir / "0.pb").exists()
        assert (events_dir / "1.pb").exists()


class TestDiscoverManifestFiles:
    def test_finds_all_studies(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "artifacts").mkdir(parents=True)
        (tracking / "study_b" / "artifacts").mkdir(parents=True)
        (tracking / "study_a" / "artifacts" / "0.manifest").touch()
        (tracking / "study_b" / "artifacts" / "0.manifest").touch()

        result = discover_manifest_files(tracking)

        assert len(result) == 2

    def test_scopes_to_single_study(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "artifacts").mkdir(parents=True)
        (tracking / "study_b" / "artifacts").mkdir(parents=True)
        (tracking / "study_a" / "artifacts" / "0.manifest").touch()
        (tracking / "study_b" / "artifacts" / "0.manifest").touch()

        result = discover_manifest_files(tracking, study="study_b")

        assert len(result) == 1

    def test_returns_empty_when_no_artifacts_dir(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)

        result = discover_manifest_files(tracking)

        assert result == []


class TestSyncArtifacts:
    def test_uploads_from_manifests(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        artifacts_dir = tracking / "study_a" / "artifacts"
        artifacts_dir.mkdir(parents=True)

        # Write a manifest with one entry
        manifest = artifacts_dir / "0.manifest"
        import json

        manifest.write_text(
            json.dumps({"key": "model.pt", "path": "/work/m.pt"}) + "\n"
        )

        mock_upload = MagicMock()
        sync_artifacts(
            tracking,
            upload_fn=mock_upload,
            project="proj",
            study="study_a",
            trial_id=0,
        )

        assert mock_upload.call_count == 1
        assert mock_upload.call_args[0][0] == "proj/study_a/0/model.pt"

    def test_noop_when_no_manifests(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)

        mock_upload = MagicMock()
        sync_artifacts(
            tracking,
            upload_fn=mock_upload,
            project="proj",
            study="study_a",
            trial_id=0,
        )

        assert mock_upload.call_count == 0
