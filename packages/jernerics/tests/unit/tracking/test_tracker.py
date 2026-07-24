import json
from pathlib import Path

import numpy as np
import pytest
from jernerics.tracking.jsonl_io import TrackingReader
from jernerics.tracking.tracker import JsonlTracker


def read_all(path: Path) -> list[dict]:
    with TrackingReader(path) as reader:
        return list(reader)


def read_events(path: Path) -> list[dict]:
    """Read all events excluding the trailing trial_end sentinel."""
    return [e for e in read_all(path) if "trial_end" not in e]


class TestLogParam:
    def test_float(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.001)

        [env] = read_events(p)
        assert env["param"]["key"] == "lr"
        assert env["param"]["value"] == {"float_val": pytest.approx(0.001)}

    def test_int(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("batch_size", 64)

        [env] = read_events(p)
        assert env["param"]["value"] == {"int_val": 64}

    def test_str(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("model", "gpt")

        [env] = read_events(p)
        assert env["param"]["value"] == {"string_val": "gpt"}

    def test_bool(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("use_amp", True)

        [env] = read_events(p)
        assert env["param"]["value"] == {"bool_val": True}

    def test_bool_before_int(self, tmp_path: Path) -> None:
        """bool is a subclass of int — must serialize as bool_val, not int_val."""
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("flag", False)

        [env] = read_events(p)
        assert env["param"]["value"] == {"bool_val": False}

    def test_unsupported_type_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p):  # noqa: SIM117
            with pytest.raises(TypeError, match="Unsupported"):
                t = JsonlTracker("project", "study", 1, p)
                t.log_param("bad", [1, 2, 3])  # ty: ignore[invalid-argument-type]


class TestLogValue:
    def test_with_step(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("loss", 0.42, step=100)

        [env] = read_events(p)
        assert env["value"]["key"] == "loss"
        assert env["value"]["value"] == pytest.approx(0.42)
        assert env["value"]["step"] == 100

    def test_without_step_or_context(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("accuracy", 0.95)

        [env] = read_events(p)
        assert env["value"]["step"] is None
        assert env["value"]["context"] == "{}"
        assert env["value"]["value"] == pytest.approx(0.95)

    def test_with_context(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("loss", 0.5, context={"seed": 3})

        [env] = read_events(p)
        assert env["value"]["context"] == '{"seed":3}'

    def test_with_step_and_context(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("loss", 0.5, step=100, context={"seed": 3, "fold": 1})

        [env] = read_events(p)
        assert env["value"]["step"] == 100
        assert env["value"]["context"] == '{"fold":1,"seed":3}'

    def test_negative_value(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("score", -3.14)

        [env] = read_events(p)
        assert env["value"]["value"] == pytest.approx(-3.14)

    def test_int_coerced_to_float(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("epochs", 10)

        [env] = read_events(p)
        val = env["value"]["value"]
        assert val == pytest.approx(10.0)
        assert isinstance(val, float)

    def test_bool_coerced_before_int(self, tmp_path: Path) -> None:
        """bool is a subclass of int — must coerce to float, not int."""
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("flag", True)

        [env] = read_events(p)
        assert env["value"]["value"] == pytest.approx(1.0)

    def test_numpy_float(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("loss", np.float64(0.32))

        [env] = read_events(p)
        assert env["value"]["value"] == pytest.approx(0.32)

    def test_numpy_int(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("tokens", np.int32(4096))  # ty: ignore[invalid-argument-type]

        [env] = read_events(p)
        assert env["value"]["value"] == pytest.approx(4096.0)

    def test_nan_becomes_null(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("nanv", float("nan"))

        [env] = read_events(p)
        assert env["value"]["value"] is None

    def test_inf_becomes_null(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("infv", float("inf"))

        [env] = read_events(p)
        assert env["value"]["value"] is None

    def test_unsupported_type_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        tracker = JsonlTracker("project", "study", 1, p)
        with pytest.raises(TypeError, match="Unsupported"):
            tracker.log_value("bad", "not a number")  # ty: ignore[invalid-argument-type]


class TestLogJson:
    def test_dict(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        data = {"x": [1, 2], "y": [3, 4]}
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_json("pareto", data)

        [env] = read_events(p)
        assert env["value"]["key"] == "pareto"
        assert "value_json" in env["value"]
        assert json.loads(env["value"]["value_json"]) == data

    def test_list(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        data = [1, 2, 3]
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_json("ranks", data)

        [env] = read_events(p)
        assert json.loads(env["value"]["value_json"]) == data

    def test_nested(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        data = {"matrix": [[1, 0], [0, 1]], "labels": ["a", "b"]}
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_json("confusion", data)

        [env] = read_events(p)
        assert json.loads(env["value"]["value_json"]) == data

    def test_with_context(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_json("summary", {"a": 1}, context={"seed": 3})

        [env] = read_events(p)
        assert env["value"]["context"] == '{"seed":3}'


class TestLogArtifact:
    def test_basic(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_artifact("model", "/work/model.pt")

        [env] = read_events(p)
        assert env["artifact"]["key"] == "model"

    def test_filename_is_basename(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_artifact("model", "/work/checkpoints/model.pt")

        [env] = read_events(p)
        assert env["artifact"]["filename"] == "model.pt"

    def test_filename_from_bare_name(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_artifact("model", "model.pt")

        [env] = read_events(p)
        assert env["artifact"]["filename"] == "model.pt"

    def test_context_default_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_artifact("model", "/work/model.pt")

        [env] = read_events(p)
        assert env["artifact"]["context"] == "{}"

    def test_with_context(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_artifact("model", "/work/model.pt", context={"seed": 3})

        [env] = read_events(p)
        assert env["artifact"]["context"] == '{"seed":3}'

    def test_writes_to_manifest(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        manifest_path = tmp_path / "1.manifest"
        with JsonlTracker("project", "study", 1, p, manifest_path=manifest_path) as t:
            t.log_artifact("model", "/work/model.pt")

        lines = manifest_path.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["key"] == "model"
        assert entry["path"] == "/work/model.pt"


class TestRunId:
    def test_run_id_in_every_envelope(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p, run_id=99) as t:
            t.log_param("lr", 0.01)
            t.log_value("loss", 0.5, step=1)
            t.log_json("summary", [1])
            t.log_artifact("model", "/work/m.pt")

        for env in read_all(p):
            assert env["run_id"] == 99

    def test_run_id_default_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("loss", 0.5)

        [env] = read_events(p)
        assert env["run_id"] == 0


class TestSeq:
    def test_seq_starts_at_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)

        [env] = read_events(p)
        assert env["seq"] == 0

    def test_seq_increments_monotonically(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)
            t.log_value("loss", 0.5)
            t.log_json("data", [1])

        envs = read_events(p)
        assert [e["seq"] for e in envs] == [0, 1, 2]


class TestTrialEnd:
    def test_close_writes_trial_end(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)

        envs = read_all(p)
        assert len(envs) == 2
        assert "param" in envs[0]
        assert "trial_end" in envs[1]
        assert envs[1]["trial_end"] == {}

    def test_trial_end_has_next_seq(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)

        envs = read_all(p)
        assert envs[0]["seq"] == 0
        assert envs[1]["seq"] == 1


class TestTimestamp:
    def test_timestamp_set(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("loss", 0.5)

        [env] = read_events(p)
        assert env["timestamp_ns"] > 0

    def test_timestamps_are_monotonic(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_value("loss", 0.5, step=1)
            t.log_value("loss", 0.3, step=2)

        envs = read_events(p)
        assert envs[1]["timestamp_ns"] >= envs[0]["timestamp_ns"]


class TestMultipleCalls:
    def test_mixed_events(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        with JsonlTracker("project", "study", 1, p) as t:
            t.log_param("lr", 0.01)
            t.log_value("loss", 0.5, step=10)
            t.log_artifact("model", "/work/m.pt")
            t.log_json("pareto", [1, 2, 3])

        envs = read_events(p)
        assert len(envs) == 4
        assert "param" in envs[0]
        assert "value" in envs[1]
        assert "artifact" in envs[2]
        assert "value" in envs[3]
