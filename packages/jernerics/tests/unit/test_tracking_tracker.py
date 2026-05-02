import json
from pathlib import Path

import pytest
from jernerics.tracking.tracker import ProtobufTracker
from jernerics.tracking.wire import TrackingReader


def read_all(path: Path) -> list:
    with TrackingReader(path) as reader:
        return list(reader)


def read_events(path: Path) -> list:
    """Read all events excluding the trailing trial_end sentinel."""
    envs = read_all(path)
    return [e for e in envs if e.WhichOneof("payload") != "trial_end"]


class TestLogParam:
    def test_float(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.001)

        [env] = read_events(p)
        assert env.study_name == "study"
        assert env.trial_id == 1
        assert env.param.key == "lr"
        assert env.param.value.float_val == pytest.approx(0.001)

    def test_int(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("batch_size", 64)

        [env] = read_events(p)
        assert env.param.value.int_val == 64

    def test_str(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("model", "gpt")

        [env] = read_events(p)
        assert env.param.value.string_val == "gpt"

    def test_bool(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("use_amp", True)

        [env] = read_events(p)
        assert env.param.value.bool_val is True

    def test_bool_before_int(self, tmp_path: Path) -> None:
        """bool is a subclass of int — must check bool first."""
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("flag", False)

        [env] = read_events(p)
        assert env.param.value.WhichOneof("value") == "bool_val"

    def test_unsupported_type_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:  # noqa: SIM117
            with pytest.raises(TypeError, match="Unsupported"):
                t.log_param("bad", [1, 2, 3])  # ty: ignore[invalid-argument-type]


class TestLogMetric:
    def test_with_step(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_metric("loss", 0.42, step=100)

        [env] = read_events(p)
        assert env.metric.key == "loss"
        assert env.metric.value == pytest.approx(0.42)
        assert env.metric.step == 100

    def test_without_step(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_metric("accuracy", 0.95)

        [env] = read_events(p)
        assert env.metric.step == -1

    def test_negative_value(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_metric("score", -3.14)

        [env] = read_events(p)
        assert env.metric.value == pytest.approx(-3.14)


class TestLogResult:
    def test_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        data = {"x": [1, 2], "y": [3, 4]}
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_result("pareto", data)

        [env] = read_events(p)
        assert env.result.key == "pareto"
        assert json.loads(env.result.value) == data

    def test_list(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        data = [1, 2, 3]
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_result("ranks", data)

        [env] = read_events(p)
        assert json.loads(env.result.value) == data

    def test_nested(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        data = {"matrix": [[1, 0], [0, 1]], "labels": ["a", "b"]}
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_result("confusion", data)

        [env] = read_events(p)
        assert json.loads(env.result.value) == data


class TestLogArtifact:
    def test_basic(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_artifact("model", "/work/model.pt")

        [env] = read_events(p)
        assert env.artifact.key == "model"

    def test_writes_to_manifest(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        manifest_path = tmp_path / "1.manifest"
        with ProtobufTracker(
            "project", "study", 1, p, manifest_path=manifest_path
        ) as t:
            t.log_artifact("model", "/work/model.pt")

        lines = manifest_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["key"] == "model"
        assert entry["path"] == "/work/model.pt"


class TestSeq:
    def test_seq_starts_at_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)

        [env] = read_events(p)
        assert env.seq == 0

    def test_seq_increments_monotonically(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)
            t.log_metric("loss", 0.5)
            t.log_result("data", [1])

        envs = read_events(p)
        assert [e.seq for e in envs] == [0, 1, 2]


class TestTrialEnd:
    def test_close_writes_trial_end(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)

        envs = read_all(p)
        assert len(envs) == 2
        assert envs[0].WhichOneof("payload") == "param"
        assert envs[1].WhichOneof("payload") == "trial_end"

    def test_trial_end_has_next_seq(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)

        envs = read_all(p)
        assert envs[0].seq == 0
        assert envs[1].seq == 1


class TestTimestamp:
    def test_timestamp_set(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_metric("loss", 0.5)

        [env] = read_events(p)
        assert env.timestamp_ns > 0

    def test_timestamps_are_monotonic(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_metric("loss", 0.5, step=1)
            t.log_metric("loss", 0.3, step=2)

        envs = read_events(p)
        assert envs[1].timestamp_ns >= envs[0].timestamp_ns


class TestMultipleCalls:
    def test_mixed_events(self, tmp_path: Path) -> None:
        p = tmp_path / "test.pb"
        with ProtobufTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)
            t.log_metric("loss", 0.5, step=10)
            t.log_artifact("model", "/work/m.pt")
            t.log_result("pareto", [1, 2, 3])

        envs = read_events(p)
        assert len(envs) == 4
        assert envs[0].WhichOneof("payload") == "param"
        assert envs[1].WhichOneof("payload") == "metric"
        assert envs[2].WhichOneof("payload") == "artifact"
        assert envs[3].WhichOneof("payload") == "result"
