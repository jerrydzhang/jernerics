import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import grpc
from jernerics.tracking.pb_io import TrackingWriter
from jernerics.tracking.stream_client import StreamClient
from jernerics_proto import (
    Envelope,
    MetricEvent,
    ParamEvent,
    TrialEndEvent,
    Value,
    tracking_pb2,
    tracking_pb2_grpc,
)


class TestGrpcChannelKeepalive:
    @patch("jernerics.tracking.grpc_channel.grpc.insecure_channel")
    def test_insecure_channel_has_keepalive_options(self, mock_insecure):
        from jernerics.tracking.grpc_channel import grpc_channel

        grpc_channel("localhost:50051")
        kwargs = mock_insecure.call_args[1]
        options = kwargs.get("options")
        assert options is not None
        opt_dict = {k: v for k, v in options}
        assert "grpc.keepalive_time_ms" in opt_dict

    @patch("jernerics.tracking.grpc_channel.grpc.secure_channel")
    def test_secure_channel_has_keepalive_options(self, mock_secure):
        from jernerics.tracking.grpc_channel import grpc_channel

        grpc_channel("server.example.com:443")
        kwargs = mock_secure.call_args[1]
        options = kwargs.get("options")
        assert options is not None
        opt_dict = {k: v for k, v in options}
        assert "grpc.keepalive_time_ms" in opt_dict


class TestApiKeyAuth:
    def test_stream_client_authenticates_with_server(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        api_key = "test-key"
        server = _start_server(tmp_path / "test.duckdb", api_key=api_key)
        stub = _make_stub()
        pb_file = tmp_path / "0.pb"

        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client = StreamClient(
            stub, pb_file, poll_interval=0.05, flush_timeout=5.0, api_key=api_key
        )
        client.start()
        client.join()
        server.stop(grace=0)

        import duckdb

        con = duckdb.connect(str(tmp_path / "test.duckdb"))
        assert _count(con, "params") == 1
        con.close()

    def test_stream_client_without_key_against_auth_server_fails(
        self, tmp_path: Path
    ) -> None:
        api_key = "test-key"
        server = _start_server(tmp_path / "test.duckdb", api_key=api_key)
        stub = _make_stub()
        pb_file = tmp_path / "0.pb"

        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client = StreamClient(
            stub, pb_file, poll_interval=0.01, flush_timeout=2.0, max_retry_time=0.5
        )
        client.start()
        client.join()
        server.stop(grace=0)

        import duckdb

        con = duckdb.connect(str(tmp_path / "test.duckdb"))
        assert _count(con, "params") == 0
        con.close()


class TestSendEventDeadline:
    def test_send_event_called_with_deadline(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        mock_stub.SendEvent.return_value = tracking_pb2.Ack()

        pb_file = tmp_path / "0.pb"
        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client = StreamClient(mock_stub, pb_file, poll_interval=0.01, flush_timeout=5.0)
        client.start()
        client.join()

        # Every SendEvent call should have a timeout/deadline argument
        for call in mock_stub.SendEvent.call_args_list:
            assert (
                call.kwargs.get("timeout") is not None
                or call[1].get("timeout") is not None
            )

    def test_deadline_exceeded_retried(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        call_count = 0

        class DeadlineExceededError(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.DEADLINE_EXCEEDED

            def details(self):
                return "deadline exceeded"

        def send_side_effect(event, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise DeadlineExceededError()
            return tracking_pb2.Ack()

        mock_stub.SendEvent.side_effect = send_side_effect

        pb_file = tmp_path / "0.pb"
        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client = StreamClient(mock_stub, pb_file, poll_interval=0.01, flush_timeout=5.0)
        client.start()
        client.join()

        assert mock_stub.SendEvent.call_count >= 3

    def test_total_retry_budget_exceeded_stops(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()

        class DeadlineExceededError(grpc.RpcError):
            def code(self):
                return grpc.StatusCode.DEADLINE_EXCEEDED

            def details(self):
                return "deadline exceeded"

        mock_stub.SendEvent.side_effect = DeadlineExceededError()

        pb_file = tmp_path / "0.pb"
        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client = StreamClient(
            mock_stub,
            pb_file,
            poll_interval=0.01,
            flush_timeout=5.0,
            max_retry_time=0.5,
        )
        client.start()
        client.join()

        # Should have stopped retrying after budget exceeded
        # Not infinite calls
        assert mock_stub.SendEvent.call_count < 100


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


class TestHappyPath:
    def test_sends_events_to_server(self, tmp_path: Path) -> None:
        server = _start_server(tmp_path / "test.duckdb")
        stub = _make_stub()
        pb_file = tmp_path / "0.pb"

        events = [
            _param_envelope(0, "lr", 0.01),
            _metric_envelope(1, "loss", 0.5, 10),
            _trial_end_envelope(2),
        ]
        with TrackingWriter(pb_file) as writer:
            for env in events:
                writer.write_envelope(env)

        client = StreamClient(stub, pb_file, poll_interval=0.05, flush_timeout=5.0)
        client.start()
        client.join()
        server.stop(grace=0)

        import duckdb

        con = duckdb.connect(str(tmp_path / "test.duckdb"))
        assert _count(con, "params") == 1
        assert _count(con, "metrics") == 1
        assert _count(con, "trial_end") == 1
        con.close()


class TestShutdown:
    def test_join_returns_after_trial_end(self, tmp_path: Path) -> None:
        server = _start_server(tmp_path / "test.duckdb")
        stub = _make_stub()
        pb_file = tmp_path / "0.pb"

        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client = StreamClient(stub, pb_file, poll_interval=0.05, flush_timeout=5.0)
        client.start()
        client.join()
        server.stop(grace=0)

        assert not client.producer_thread.is_alive()
        assert not client.consumer.is_alive()

    def test_join_timeout_without_trial_end(self, tmp_path: Path) -> None:
        server = _start_server(tmp_path / "test.duckdb")
        stub = _make_stub()
        pb_file = tmp_path / "0.pb"

        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))

        client = StreamClient(stub, pb_file, poll_interval=0.05, flush_timeout=0.5)
        client.start()
        client.join()

        assert client.producer_thread.is_alive() or client.consumer.is_alive()
        server.stop(grace=0)


class TestRetryOnFailure:
    def test_retries_and_succeeds(self, tmp_path: Path) -> None:
        mock_stub = MagicMock()
        call_count = 0

        def send_side_effect(event, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise FakeRpcError()
            return tracking_pb2.Ack()

        mock_stub.SendEvent.side_effect = send_side_effect

        pb_file = tmp_path / "0.pb"
        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client = StreamClient(mock_stub, pb_file, poll_interval=0.01, flush_timeout=5.0)
        client.start()
        client.join()

        assert mock_stub.SendEvent.call_count >= 3


class TestDeferredFileCreation:
    def test_waits_for_file_to_exist(self, tmp_path: Path) -> None:
        server = _start_server(tmp_path / "test.duckdb")
        stub = _make_stub()
        pb_file = tmp_path / "0.pb"

        client = StreamClient(stub, pb_file, poll_interval=0.05, flush_timeout=5.0)
        client.start()

        time.sleep(0.2)
        assert not pb_file.exists()

        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        client.join()
        server.stop(grace=0)

        assert not client.producer_thread.is_alive()


class TestPartialEvent:
    def test_truncated_event_does_not_crash_producer(self, tmp_path: Path) -> None:
        server = _start_server(tmp_path / "test.duckdb")
        stub = _make_stub()
        pb_file = tmp_path / "0.pb"

        from jernerics.tracking.pb_io import encode_varint

        with TrackingWriter(pb_file) as writer:
            writer.write_envelope(_param_envelope(0, "lr", 0.01))
            writer.write_envelope(_trial_end_envelope(1))

        with open(pb_file, "ab") as f:
            f.write(encode_varint(1000) + b"\x00\x00\x00\x00\x00")

        client = StreamClient(stub, pb_file, poll_interval=0.05, flush_timeout=5.0)
        client.start()
        client.join()
        server.stop(grace=0)

        import duckdb

        con = duckdb.connect(str(tmp_path / "test.duckdb"))
        assert _count(con, "params") == 1
        assert _count(con, "trial_end") == 1
        con.close()


def _count(con, table: str) -> int:
    row = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    assert row is not None
    return row[0]


def _start_server(db_path: Path, api_key: str | None = None) -> grpc.Server:
    from jernerics_server.server import serve

    return serve(db_path, port=50053, api_key=api_key)


def _make_stub() -> tracking_pb2_grpc.TrackingServiceStub:
    channel = grpc.insecure_channel("localhost:50053")
    return tracking_pb2_grpc.TrackingServiceStub(channel)
