from __future__ import annotations

from pathlib import Path

import pytest
from jernerics.tracking.wire import (
    TrackingReader,
    TrackingWriter,
    decode_varint,
    encode_varint,
)
from jernerics_proto import Envelope


def make_param_envelope(
    project: str = "testproj",
    study_name: str = "test",
    trial_id: int = 1,
    key: str = "lr",
    value: float = 0.001,
    timestamp_ns: int = 1000,
) -> Envelope:
    env = Envelope(
        project=project,
        study_name=study_name,
        trial_id=trial_id,
        timestamp_ns=timestamp_ns,
    )
    env.param.key = key
    env.param.value.float_val = value
    return env


def make_metric_envelope(
    project: str = "testproj",
    study_name: str = "test",
    trial_id: int = 1,
    key: str = "loss",
    value: float = 0.42,
    step: int = 100,
    timestamp_ns: int = 2000,
) -> Envelope:
    env = Envelope(
        project=project,
        study_name=study_name,
        trial_id=trial_id,
        timestamp_ns=timestamp_ns,
    )
    env.metric.key = key
    env.metric.value = value
    env.metric.step = step
    return env


def make_result_envelope(
    project: str = "testproj",
    study_name: str = "test",
    trial_id: int = 1,
    key: str = "pareto",
    value: str = '{"x": [1, 2], "y": [3, 4]}',
    timestamp_ns: int = 3000,
) -> Envelope:
    env = Envelope(
        project=project,
        study_name=study_name,
        trial_id=trial_id,
        timestamp_ns=timestamp_ns,
    )
    env.result.key = key
    env.result.value = value
    return env


def make_artifact_envelope(
    project: str = "testproj",
    study_name: str = "test",
    trial_id: int = 1,
    key: str = "model",
    local_path: str = "/work/model.pt",
    timestamp_ns: int = 4000,
) -> Envelope:
    env = Envelope(
        project=project,
        study_name=study_name,
        trial_id=trial_id,
        timestamp_ns=timestamp_ns,
    )
    env.artifact.key = key
    env.artifact.local_path = local_path
    return env


def make_sweep_meta_envelope(
    project: str = "testproj",
    study_name: str = "test",
    git_hash: str = "abc123",
    config: str = "base = {}",
    timestamp_ns: int = 0,
) -> Envelope:
    env = Envelope(
        project=project, study_name=study_name, trial_id=0, timestamp_ns=timestamp_ns
    )
    env.sweep_meta.git_hash = git_hash
    env.sweep_meta.config = config
    return env


def read_all(path: Path) -> list[Envelope]:
    with TrackingReader(path) as reader:
        return list(reader)


class TestEncodeVarint:
    def test_single_byte(self):
        assert encode_varint(0) == b"\x00"
        assert encode_varint(1) == b"\x01"
        assert encode_varint(127) == b"\x7f"

    def test_two_bytes(self):
        assert encode_varint(128) == b"\x80\x01"
        assert encode_varint(150) == b"\x96\x01"
        assert encode_varint(300) == b"\xac\x02"

    def test_large_value(self):
        encoded = encode_varint(2**20)
        decoded = int.from_bytes(
            encoded, "little"
        )  # not a true varint decode, just check size
        assert len(encoded) == 3

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="Negative"):
            encode_varint(-1)


class TestDecodeVarint:
    def test_empty_stream_returns_none(self, tmp_path: Path):
        p = tmp_path / "empty.bin"
        p.write_bytes(b"")
        with open(p, "rb") as f:
            assert decode_varint(f) is None

    def test_round_trip(self, tmp_path: Path):
        for value in [0, 1, 127, 128, 150, 300, 2**21]:
            p = tmp_path / f"varint_{value}.bin"
            p.write_bytes(encode_varint(value))
            with open(p, "rb") as f:
                assert decode_varint(f) == value

    def test_truncated_varint(self, tmp_path: Path):
        p = tmp_path / "truncated.bin"
        p.write_bytes(b"\x80")  # continuation bit set, but no more bytes
        with open(p, "rb") as f, pytest.raises(EOFError, match="Truncated"):
            decode_varint(f)


class TestSingleEnvelope:
    def test_param(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        original = make_param_envelope()

        with TrackingWriter(p) as writer:
            writer.write_envelope(original)

        [result] = read_all(p)
        assert result.project == "testproj"
        assert result.study_name == "test"
        assert result.trial_id == 1
        assert result.WhichOneof("payload") == "param"
        assert result.param.key == "lr"
        assert result.param.value.float_val == pytest.approx(0.001)

    def test_metric(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        original = make_metric_envelope()

        with TrackingWriter(p) as writer:
            writer.write_envelope(original)

        [result] = read_all(p)
        assert result.WhichOneof("payload") == "metric"
        assert result.metric.key == "loss"
        assert result.metric.value == pytest.approx(0.42)
        assert result.metric.step == 100

    def test_metric_no_step(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        env = Envelope(
            project="testproj", study_name="test", trial_id=1, timestamp_ns=2000
        )
        env.metric.key = "accuracy"
        env.metric.value = 0.95

        with TrackingWriter(p) as writer:
            writer.write_envelope(env)

        [result] = read_all(p)
        assert result.metric.step == 0  # proto3 default

    def test_result(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        original = make_result_envelope()

        with TrackingWriter(p) as writer:
            writer.write_envelope(original)

        [result] = read_all(p)
        assert result.WhichOneof("payload") == "result"
        assert result.result.key == "pareto"
        assert result.result.value == '{"x": [1, 2], "y": [3, 4]}'

    def test_artifact(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        original = make_artifact_envelope()

        with TrackingWriter(p) as writer:
            writer.write_envelope(original)

        [result] = read_all(p)
        assert result.WhichOneof("payload") == "artifact"
        assert result.artifact.key == "model"
        assert result.artifact.local_path == "/work/model.pt"

    def test_sweep_meta(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        original = make_sweep_meta_envelope()

        with TrackingWriter(p) as writer:
            writer.write_envelope(original)

        [result] = read_all(p)
        assert result.WhichOneof("payload") == "sweep_meta"
        assert result.sweep_meta.git_hash == "abc123"
        assert result.sweep_meta.config == "base = {}"


class TestMultipleEnvelopes:
    def test_preserves_order(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        with TrackingWriter(p) as writer:
            for i in range(5):
                writer.write_envelope(
                    make_metric_envelope(
                        key="loss", value=float(i), step=i, timestamp_ns=i
                    )
                )

        results = read_all(p)
        assert len(results) == 5
        for i, env in enumerate(results):
            assert env.metric.key == "loss"
            assert env.metric.value == pytest.approx(float(i))
            assert env.metric.step == i

    def test_mixed_types(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        with TrackingWriter(p) as writer:
            writer.write_envelope(make_param_envelope(key="lr", value=0.01))
            writer.write_envelope(make_metric_envelope(key="loss", value=0.5, step=10))
            writer.write_envelope(
                make_artifact_envelope(key="model", local_path="/work/m.pt")
            )
            writer.write_envelope(make_result_envelope(key="pareto", value="[]"))

        results = read_all(p)
        assert len(results) == 4
        assert results[0].WhichOneof("payload") == "param"
        assert results[1].WhichOneof("payload") == "metric"
        assert results[2].WhichOneof("payload") == "artifact"
        assert results[3].WhichOneof("payload") == "result"


class TestAppendMode:
    def test_reopen_preserves_data(self, tmp_path: Path):
        p = tmp_path / "test.pb"

        with TrackingWriter(p) as writer:
            writer.write_envelope(make_param_envelope(key="lr", value=0.01))

        with TrackingWriter(p) as writer:
            writer.write_envelope(make_metric_envelope(key="loss", value=0.5))

        results = read_all(p)
        assert len(results) == 2
        assert results[0].param.key == "lr"
        assert results[1].metric.key == "loss"


class TestCorruptedFile:
    def test_truncated_payload(self, tmp_path: Path):
        p = tmp_path / "test.pb"
        # Write a valid envelope
        with TrackingWriter(p) as writer:
            writer.write_envelope(make_param_envelope())

        # Append a varint claiming 1000 bytes, but only give 5
        with open(p, "ab") as f:
            f.write(encode_varint(1000) + b"\x00\x00\x00\x00\x00")

        with pytest.raises(EOFError, match="Truncated envelope"):
            read_all(p)
