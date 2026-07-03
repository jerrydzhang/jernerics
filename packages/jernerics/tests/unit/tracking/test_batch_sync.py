import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from jernerics.tracking.batch_sync import (
    ReplayResult,
    _replay_file,
    discover_jsonl_files,
    discover_manifest_files,
    replay_tracking,
    sync_artifacts,
)
from jernerics.tracking.jsonl_io import TrackingWriter

BASE_URL = "http://localhost:8000"


def _param_envelope(seq: int, key: str, value: float) -> dict:
    return {
        "project": "p",
        "study_name": "s",
        "trial_id": 0,
        "timestamp_ns": 1000 + seq,
        "seq": seq,
        "param": {"key": key, "value": {"float_val": value}},
    }


def _metric_envelope(seq: int, key: str, value: float, step: int) -> dict:
    return {
        "project": "p",
        "study_name": "s",
        "trial_id": 0,
        "timestamp_ns": 1000 + seq,
        "seq": seq,
        "metric": {"key": key, "value": value, "step": step},
    }


def _trial_end_envelope(seq: int) -> dict:
    return {
        "project": "p",
        "study_name": "s",
        "trial_id": 0,
        "timestamp_ns": 1000 + seq,
        "seq": seq,
        "trial_end": {},
    }


def _write_events(path: Path, events: list[dict]) -> None:
    with TrackingWriter(path) as writer:
        for event in events:
            writer.write_envelope(event)


def _ok_response() -> MagicMock:
    """A mocked httpx.Response: raise_for_status is a no-op, status 200."""
    return MagicMock(status_code=200)


class TestReplayFileWithAuth:
    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_sends_bearer_header_with_api_key(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        _write_events(events_file, [_param_envelope(0, "lr", 0.01)])

        result = _replay_file(events_file, BASE_URL, "secret", max_retries=3)

        assert result.error is None
        assert mock_post.call_args.kwargs["headers"] == {
            "authorization": "Bearer secret"
        }

    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_no_header_without_api_key(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        _write_events(events_file, [_param_envelope(0, "lr", 0.01)])

        result = _replay_file(events_file, BASE_URL, None, max_retries=3)

        assert result.error is None
        assert mock_post.call_args.kwargs["headers"] is None


class TestDiscoverJsonlFiles:
    def test_finds_all_studies(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_b" / "events").mkdir(parents=True)
        (tracking / "study_a" / "events" / "0.jsonl").touch()
        (tracking / "study_a" / "events" / "1.jsonl").touch()
        (tracking / "study_b" / "events" / "0.jsonl").touch()

        result = discover_jsonl_files(tracking)

        names = [p.name for p in result]
        assert names == ["0.jsonl", "1.jsonl", "0.jsonl"]

    def test_scopes_to_single_study(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_b" / "events").mkdir(parents=True)
        (tracking / "study_a" / "events" / "0.jsonl").touch()
        (tracking / "study_b" / "events" / "0.jsonl").touch()

        result = discover_jsonl_files(tracking, study="study_b")

        assert len(result) == 1
        assert result[0].parent.parent.name == "study_b"

    def test_returns_empty_for_no_files(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        tracking.mkdir()

        result = discover_jsonl_files(tracking)

        assert result == []

    def test_ignores_non_jsonl_files(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_a" / "events" / "0.jsonl").touch()
        (tracking / "study_a" / "events" / "0.db").touch()

        result = discover_jsonl_files(tracking)

        assert len(result) == 1


class TestReplayFile:
    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_sends_all_events(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        events = [
            _param_envelope(0, "lr", 0.01),
            _metric_envelope(1, "loss", 0.5, 10),
            _trial_end_envelope(2),
        ]
        _write_events(events_file, events)

        result = _replay_file(events_file, BASE_URL, None, max_retries=3)

        assert result.events_sent == 3
        assert result.events_total == 3
        assert result.error is None
        assert mock_post.call_count == 3
        for call, env in zip(mock_post.call_args_list, events, strict=False):
            assert call.args[0] == f"{BASE_URL}/ingest"
            assert call.kwargs["json"] == env
            assert call.kwargs["headers"] is None

    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_retries_on_failure(self, mock_post, tmp_path: Path) -> None:
        call_count = 0

        def post_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise httpx.ConnectError("boom")
            return _ok_response()

        mock_post.side_effect = post_side_effect

        events_file = tmp_path / "0.jsonl"
        _write_events(events_file, [_param_envelope(0, "lr", 0.01)])

        with patch("jernerics.tracking.batch_sync._RETRY_BASE_INTERVAL", 0.01):
            result = _replay_file(events_file, BASE_URL, None, max_retries=5)

        assert result.events_sent == 1
        assert result.error is None
        assert mock_post.call_count == 3

    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_records_error_on_max_retries_exceeded(
        self, mock_post, tmp_path: Path
    ) -> None:
        mock_post.side_effect = httpx.ConnectError("boom")

        events_file = tmp_path / "0.jsonl"
        _write_events(
            events_file,
            [_param_envelope(0, "lr", 0.01), _metric_envelope(1, "loss", 0.5, 10)],
        )

        with patch("jernerics.tracking.batch_sync._RETRY_BASE_INTERVAL", 0.01):
            result = _replay_file(events_file, BASE_URL, None, max_retries=2)

        assert result.events_sent == 0
        assert result.error is not None
        assert result.events_total == 2


class TestReplayTracking:
    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_replays_all_files_and_deletes_on_success(
        self, mock_post, tmp_path: Path
    ) -> None:
        mock_post.return_value = _ok_response()
        tracking = tmp_path / "tracking"
        events_dir = tracking / "study_a" / "events"
        events_dir.mkdir(parents=True)

        _write_events(events_dir / "0.jsonl", [_param_envelope(0, "lr", 0.01)])
        _write_events(events_dir / "1.jsonl", [_metric_envelope(0, "loss", 0.5, 10)])

        result = replay_tracking(tracking, BASE_URL, max_workers=2, max_retries=3)

        assert result.files_processed == 2
        assert result.events_sent == 2
        assert result.errors == []
        assert not (events_dir / "0.jsonl").exists()
        assert not (events_dir / "1.jsonl").exists()

    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_scopes_to_study(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        tracking = tmp_path / "tracking"
        (tracking / "study_a" / "events").mkdir(parents=True)
        (tracking / "study_b" / "events").mkdir(parents=True)
        _write_events(
            tracking / "study_a" / "events" / "0.jsonl",
            [_param_envelope(0, "x", 1.0)],
        )
        _write_events(
            tracking / "study_b" / "events" / "0.jsonl",
            [_param_envelope(0, "y", 2.0)],
        )

        result = replay_tracking(
            tracking, BASE_URL, study="study_b", max_workers=2, max_retries=3
        )

        assert result.files_processed == 1
        assert result.events_sent == 1
        assert not (tracking / "study_b" / "events" / "0.jsonl").exists()
        assert (tracking / "study_a" / "events" / "0.jsonl").exists()

    def test_returns_empty_result_for_no_files(self, tmp_path: Path) -> None:
        tracking = tmp_path / "tracking"
        tracking.mkdir()

        result = replay_tracking(tracking, BASE_URL, max_retries=3)

        assert result == ReplayResult()
        assert not result.errors

    @patch("jernerics.tracking.batch_sync.httpx.post")
    def test_records_partial_failure_and_keeps_files(
        self, mock_post, tmp_path: Path
    ) -> None:
        tracking = tmp_path / "tracking"
        events_dir = tracking / "study_a" / "events"
        events_dir.mkdir(parents=True)

        # File 0 replays cleanly; file 1 fails every POST permanently.
        _write_events(events_dir / "0.jsonl", [_param_envelope(0, "ok", 1.0)])
        _write_events(events_dir / "1.jsonl", [_param_envelope(0, "fail", 2.0)])

        def post_side_effect(*args, **kwargs):
            event = kwargs["json"]
            if event.get("param", {}).get("key") == "fail":
                raise httpx.ConnectError("boom")
            return _ok_response()

        mock_post.side_effect = post_side_effect

        with patch("jernerics.tracking.batch_sync._RETRY_BASE_INTERVAL", 0.01):
            result = replay_tracking(tracking, BASE_URL, max_workers=2, max_retries=3)

        assert result.files_processed == 2
        assert len(result.errors) == 1
        assert result.events_sent == 1
        # Errors present -> no files deleted.
        assert (events_dir / "0.jsonl").exists()
        assert (events_dir / "1.jsonl").exists()


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
