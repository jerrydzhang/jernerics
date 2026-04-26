from __future__ import annotations

import json
from pathlib import Path

import pytest

from jernerics.tracking.store import TrackingReader
from jernerics.tracking.tracker import ProtobufTracker


def read_all(path: Path) -> list:
    with TrackingReader(path) as reader:
        return list(reader)


class TestLogParam:
    def test_float(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_param("lr", 0.001)

        [env] = read_all(p)
        assert env.study_name == "study"
        assert env.trial_id == 1
        assert env.param.key == "lr"
        assert env.param.value.float_val == pytest.approx(0.001)

    def test_int(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_param("batch_size", 64)

        [env] = read_all(p)
        assert env.param.value.int_val == 64

    def test_str(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_param("model", "gpt")

        [env] = read_all(p)
        assert env.param.value.string_val == "gpt"

    def test_bool(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_param("use_amp", True)

        [env] = read_all(p)
        assert env.param.value.bool_val is True

    def test_bool_before_int(self, tmp_path: Path) -> None:
        """bool is a subclass of int — must check bool first."""
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_param("flag", False)

        [env] = read_all(p)
        assert env.param.value.WhichOneof("value") == "bool_val"

    def test_unsupported_type_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:  # noqa: SIM117
            with pytest.raises(ValueError, match="Unsupported"):
                t.log_param("bad", [1, 2, 3])  # type: ignore[arg-type]


class TestLogMetric:
    def test_with_step(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_metric("loss", 0.42, step=100)

        [env] = read_all(p)
        assert env.metric.key == "loss"
        assert env.metric.value == pytest.approx(0.42)
        assert env.metric.step == 100

    def test_without_step(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_metric("accuracy", 0.95)

        [env] = read_all(p)
        assert env.metric.step == -1

    def test_negative_value(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_metric("score", -3.14)

        [env] = read_all(p)
        assert env.metric.value == pytest.approx(-3.14)


class TestLogResult:
    def test_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        data = {"x": [1, 2], "y": [3, 4]}
        with ProtobufTracker("study", 1, p) as t:
            t.log_result("pareto", data)

        [env] = read_all(p)
        assert env.result.key == "pareto"
        assert json.loads(env.result.value) == data

    def test_list(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        data = [1, 2, 3]
        with ProtobufTracker("study", 1, p) as t:
            t.log_result("ranks", data)

        [env] = read_all(p)
        assert json.loads(env.result.value) == data

    def test_nested(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        data = {"matrix": [[1, 0], [0, 1]], "labels": ["a", "b"]}
        with ProtobufTracker("study", 1, p) as t:
            t.log_result("confusion", data)

        [env] = read_all(p)
        assert json.loads(env.result.value) == data


class TestLogArtifact:
    def test_basic(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_artifact("model", "/work/model.pt")

        [env] = read_all(p)
        assert env.artifact.key == "model"
        assert env.artifact.local_path == "/work/model.pt"


class TestTimestamp:
    def test_timestamp_set(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_metric("loss", 0.5)

        [env] = read_all(p)
        assert env.timestamp_ns > 0

    def test_timestamps_are_monotonic(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_metric("loss", 0.5, step=1)
            t.log_metric("loss", 0.3, step=2)

        envs = read_all(p)
        assert envs[1].timestamp_ns >= envs[0].timestamp_ns


class TestMultipleCalls:
    def test_mixed_events(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("study", 1, p) as t:
            t.log_param("lr", 0.01)
            t.log_metric("loss", 0.5, step=10)
            t.log_artifact("model", "/work/m.pt")
            t.log_result("pareto", [1, 2, 3])

        envs = read_all(p)
        assert len(envs) == 4
        assert envs[0].WhichOneof("payload") == "param"
        assert envs[1].WhichOneof("payload") == "metric"
        assert envs[2].WhichOneof("payload") == "artifact"
        assert envs[3].WhichOneof("payload") == "result"
