import json

import pytest
from jernerics.trial_context import (
    ConsoleTracker,
    is_job,
    trial_config,
    trial_tracker,
)


def _read_events(tmp_path, trial_number=7):
    path = tmp_path / "tracking" / "events" / f"{trial_number}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


class TestIsJob:
    def test_returns_false_without_trial_config_env(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)

        assert is_job() is False

    def test_returns_true_with_trial_config_env(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))

        assert is_job() is True


class TestTrialConfig:
    def test_reads_json_in_job_mode(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text('{"lr": 0.1, "epochs": 3}')
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))

        assert trial_config() == {"lr": 0.1, "epochs": 3}

    def test_returns_defaults_as_is_in_standalone_mode(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)
        defaults = {"lr": 0.1}

        assert trial_config(defaults) is defaults

    def test_raises_when_standalone_without_defaults(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)

        with pytest.raises(ValueError):
            trial_config()

    def test_raises_when_job_config_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(tmp_path / "missing.json"))

        with pytest.raises(RuntimeError):
            trial_config()


class TestTrialTracker:
    def test_returns_console_tracker_in_standalone_mode(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)

        assert isinstance(trial_tracker(), ConsoleTracker)


@pytest.fixture
def job_tracker(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}")
    monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))
    monkeypatch.setenv("JERNERICS_TRACKING_DIR", str(tmp_path / "tracking"))
    monkeypatch.setenv("JERNERICS_PROJECT_NAME", "proj")
    monkeypatch.setenv("JERNERICS_STUDY_NAME", "study")
    monkeypatch.setenv("JERNERICS_TRIAL_NUMBER", "7")
    monkeypatch.setenv("JERNERICS_RUN_ID", "123")
    return trial_tracker(), tmp_path


class TestJobTracker:
    def test_logs_numeric_observations_as_scalars(self, job_tracker):
        tracker, tmp_path = job_tracker
        tracker.log_value("loss", 0.5, step=3)
        tracker.log_value("acc", 0.9)

        loss, acc = _read_events(tmp_path)
        assert loss["value"]["key"] == "loss"
        assert loss["value"]["value"] == pytest.approx(0.5)
        assert loss["value"]["step"] == 3
        assert "value_json" not in loss["value"]
        assert acc["value"]["key"] == "acc"
        assert acc["value"]["value"] == pytest.approx(0.9)
        assert acc["value"]["step"] is None

    def test_logs_structured_and_boolean_observations_as_value_json(self, job_tracker):
        tracker, tmp_path = job_tracker
        tracker.log_value("pred", {"a": 1}, step=5)
        tracker.log_value("flag", True)

        pred, flag = _read_events(tmp_path)
        assert pred["value"]["key"] == "pred"
        assert json.loads(pred["value"]["value_json"]) == {"a": 1}
        assert pred["value"]["step"] == 5
        assert flag["value"]["key"] == "flag"
        assert json.loads(flag["value"]["value_json"]) is True
        assert flag["value"]["step"] is None

    def test_logs_artifact_event_and_manifest(self, job_tracker):
        tracker, tmp_path = job_tracker
        artifact = tmp_path / "model.pt"
        artifact.write_bytes(b"weights")
        tracker.log_artifact("model", str(artifact))

        [event] = _read_events(tmp_path)
        assert event["artifact"]["key"] == "model"
        assert event["artifact"]["filename"] == "model.pt"

        manifest = tmp_path / "tracking" / "artifacts" / "7.manifest"
        entries = [json.loads(line) for line in manifest.read_text().splitlines()]
        assert entries == [{"key": "model", "path": str(artifact)}]

    def test_finish_records_results_and_closes_tracking(self, job_tracker):
        tracker, tmp_path = job_tracker
        tracker.finish({"score": 1.0})

        [event] = _read_events(tmp_path)
        assert event["value"]["key"] == "results"
        assert json.loads(event["value"]["value_json"]) == {"score": 1.0}

        with pytest.raises(ValueError):
            tracker.log_value("late", 1.0)


class TestConsoleTracker:
    def test_log_param_prints_param(self, capsys):
        ConsoleTracker().log_param("model", "mlp")

        assert capsys.readouterr().out == "param: model=mlp\n"

    def test_numeric_observation_without_step(self, capsys):
        ConsoleTracker().log_value("loss", 0.25)

        assert capsys.readouterr().out == "[value] loss=0.25\n"

    def test_numeric_observation_with_step(self, capsys):
        ConsoleTracker().log_value("loss", 0.25, step=3)

        assert capsys.readouterr().out == "[step 3] loss=0.25\n"

    def test_non_numeric_observations_serialize_as_json(self, capsys):
        tracker = ConsoleTracker()
        tracker.log_value("note", "ok")
        tracker.log_value("flag", True)
        tracker.log_value("none", None)
        tracker.log_value("xs", [1, 2])
        tracker.log_value("d", {"a": 1})

        out = capsys.readouterr().out
        assert '[value] note="ok"\n' in out
        assert "[value] flag=true\n" in out
        assert "[value] none=null\n" in out
        assert "[value] xs=[1, 2]\n" in out
        assert '[value] d={"a": 1}\n' in out

    def test_artifact_output(self, capsys):
        ConsoleTracker().log_artifact("model.pt", "/tmp/model.pt")

        assert capsys.readouterr().out == "[artifact] model.pt=/tmp/model.pt\n"

    def test_finish_prints_summary(self, capsys):
        ConsoleTracker().finish({"score": 0.9, "status": "ok"})

        assert capsys.readouterr().out == "results:\n  score=0.9\n  status=ok\n"


class TestPublicSurface:
    @pytest.mark.parametrize("method", ["log_text", "log_json", "log_sweep_meta"])
    def test_console_lacks_backend_methods(self, method):
        assert not hasattr(ConsoleTracker(), method)

    @pytest.mark.parametrize("method", ["log_text", "log_json", "log_sweep_meta"])
    def test_job_tracker_lacks_backend_methods(self, job_tracker, method):
        tracker, _ = job_tracker
        assert not hasattr(tracker, method)
