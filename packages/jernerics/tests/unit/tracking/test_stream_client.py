import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from jernerics.tracking.jsonl_io import TrackingWriter
from jernerics.tracking.stream_client import StreamClient

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


def _write_envelopes(path: Path, envelopes: list[dict]) -> None:
    with TrackingWriter(path) as writer:
        for env in envelopes:
            writer.write_envelope(env)


def _ok_response() -> MagicMock:
    """A mocked httpx.Response: raise_for_status is a no-op, status 200."""
    return MagicMock(status_code=200)


class TestHappyPath:
    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_posts_each_envelope_to_ingest(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        envelopes = [
            _param_envelope(0, "lr", 0.01),
            _metric_envelope(1, "loss", 0.5, 10),
            _trial_end_envelope(2),
        ]
        _write_envelopes(events_file, envelopes)

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=5.0,
        )
        client.start()
        client.join()

        assert mock_post.call_count == len(envelopes)
        for call, env in zip(mock_post.call_args_list, envelopes, strict=False):
            assert call.args[0] == f"{BASE_URL}/ingest"
            assert call.kwargs["json"] == env
            # No api key -> no auth header.
            assert call.kwargs["headers"] is None

    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_trial_end_terminates_shipping(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        _write_envelopes(
            events_file, [_param_envelope(0, "lr", 0.01), _trial_end_envelope(1)]
        )

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=5.0,
        )
        client.start()
        client.join()

        # Both threads exit once trial_end has shipped.
        assert not client.producer_thread.is_alive()
        assert not client.consumer.is_alive()
        # Exactly one POST per envelope and no more.
        assert mock_post.call_count == 2


class TestSendDeadline:
    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_each_post_carries_send_deadline(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        _write_envelopes(
            events_file, [_param_envelope(0, "lr", 0.01), _trial_end_envelope(1)]
        )

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=5.0,
            send_deadline=12.5,
        )
        client.start()
        client.join()

        for call in mock_post.call_args_list:
            assert call.kwargs["timeout"] == pytest.approx(client.send_deadline)


class TestAuthHeader:
    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_sends_bearer_header_when_api_key_set(
        self, mock_post, tmp_path: Path
    ) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        _write_envelopes(
            events_file, [_param_envelope(0, "lr", 0.01), _trial_end_envelope(1)]
        )

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            api_key="secret-key",
            poll_interval=0.01,
            flush_timeout=5.0,
        )
        client.start()
        client.join()

        for call in mock_post.call_args_list:
            assert call.kwargs["headers"] == {"authorization": "Bearer secret-key"}

    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_no_header_without_api_key(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        _write_envelopes(
            events_file, [_param_envelope(0, "lr", 0.01), _trial_end_envelope(1)]
        )

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=5.0,
        )
        client.start()
        client.join()

        for call in mock_post.call_args_list:
            assert call.kwargs["headers"] is None


class TestShutdown:
    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_join_returns_after_trial_end(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        _write_envelopes(
            events_file, [_param_envelope(0, "lr", 0.01), _trial_end_envelope(1)]
        )

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=5.0,
        )
        client.start()
        client.join()

        assert not client.producer_thread.is_alive()
        assert not client.consumer.is_alive()

    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_threads_stay_alive_without_trial_end(
        self, mock_post, tmp_path: Path
    ) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"
        # No trial_end: the producer keeps tailing the file forever.
        _write_envelopes(events_file, [_param_envelope(0, "lr", 0.01)])

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=0.5,
        )
        client.start()
        client.join()

        # join() times out; the producer is still polling.
        assert client.producer_thread.is_alive()


class TestRetryOnFailure:
    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_retries_on_connect_error_then_succeeds(
        self, mock_post, tmp_path: Path
    ) -> None:
        call_count = 0

        def post_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise httpx.ConnectError("boom")
            return _ok_response()

        mock_post.side_effect = post_side_effect

        events_file = tmp_path / "0.jsonl"
        _write_envelopes(
            events_file, [_param_envelope(0, "lr", 0.01), _trial_end_envelope(1)]
        )

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=5.0,
            max_retry_time=10.0,
        )
        client.start()
        client.join()

        # param event: 2 failed attempts + 1 success; trial_end: 1 success.
        assert mock_post.call_count == 4
        # trial_end shipped -> consumer exited cleanly.
        assert not client.consumer.is_alive()


class TestDeferredFileCreation:
    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_waits_for_file_to_exist(self, mock_post, tmp_path: Path) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.05,
            flush_timeout=5.0,
        )
        client.start()

        time.sleep(0.2)
        assert not events_file.exists()
        assert client.producer_thread.is_alive()

        _write_envelopes(
            events_file, [_param_envelope(0, "lr", 0.01), _trial_end_envelope(1)]
        )

        client.join()

        assert not client.producer_thread.is_alive()
        assert mock_post.call_count == 2


class TestPartialEvent:
    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_truncated_trailing_line_does_not_crash_producer(
        self, mock_post, tmp_path: Path
    ) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"

        with TrackingWriter(events_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        # Append a truncated JSON line (writer crashed mid-flush). It sits
        # after trial_end, so the producer never reaches it; this confirms a
        # partial line trailing valid events doesn't trip the producer.
        with open(events_file, "a") as f:
            f.write('{"project":"p","study_name"')

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.01,
            flush_timeout=5.0,
        )
        client.start()
        client.join()

        assert not client.consumer.is_alive()
        # The two complete envelopes shipped; the truncated line was ignored.
        assert mock_post.call_count == 2

    @patch("jernerics.tracking.stream_client.httpx.post")
    def test_partial_line_is_waited_for_not_crashed(
        self, mock_post, tmp_path: Path
    ) -> None:
        mock_post.return_value = _ok_response()
        events_file = tmp_path / "0.jsonl"

        # A partial line (writer crashed mid-flush): no newline, invalid JSON.
        with open(events_file, "w") as f:
            f.write('{"project":"p","study_name"')

        client = StreamClient(
            base_url=BASE_URL,
            path=events_file,
            poll_interval=0.02,
            flush_timeout=0.5,
        )
        client.start()

        time.sleep(0.2)
        # The producer did not crash and has shipped nothing yet.
        assert client.producer_thread.is_alive()
        assert mock_post.call_count == 0

        client.join()

        # No trial_end -> the producer keeps waiting on the partial line,
        # past join()'s timeout. Graceful, not crashed.
        assert client.producer_thread.is_alive()
