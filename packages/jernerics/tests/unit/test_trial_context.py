import json
import os
import uuid

import pytest
from jernerics.tracking.jsonl_io import TrackingReader
from jernerics.trial_context import (
    ConsoleTracker,
    is_job,
    trial_config,
    trial_tracker,
)


def _read_events(tmp_path, trial_number=7):
    path = tmp_path / "tracking" / "events" / f"{trial_number}.jsonl"
    with TrackingReader(path) as reader:
        return list(reader)


class TestIsJob:
    def test_returns_false_without_trial_config_env(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)

        assert is_job() is False

    def test_returns_true_with_trial_config_env(self, monkeypatch):
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", "/tmp/config.json")

        assert is_job() is True


class TestTrialConfig:
    def test_reads_json_in_job_mode(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text('{"lr": 0.1}')
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))

        assert trial_config() == {"lr": 0.1}

    def test_raises_without_defaults_outside_job(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)

        with pytest.raises(ValueError):
            trial_config()

    def test_non_dict_config_raises_type_error(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("[1, 2]")
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))

        with pytest.raises(TypeError):
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
    monkeypatch.setenv("JERNERICS_TRIAL_ID", str(uuid.uuid4()))
    monkeypatch.setenv("JERNERICS_EXECUTION_ID", str(uuid.uuid4()))
    return trial_tracker(), tmp_path


class TestJobTracker:
    def test_logs_numeric_observations_as_scalar_values(self, job_tracker):
        tracker, tmp_path = job_tracker
        tracker.log_value("loss", 0.5, step=3)
        tracker.log_value("acc", 0.9)

        loss, acc = _read_events(tmp_path)

        assert loss.tag == "value"
        assert loss.key == "loss"
        assert loss.value == pytest.approx(0.5)
        assert loss.step == 3
        assert acc.key == "acc"
        assert acc.value == pytest.approx(0.9)
        assert acc.step == 0

    def test_logs_structured_and_boolean_observations_as_observations(
        self, job_tracker
    ):
        tracker, tmp_path = job_tracker
        tracker.log_value("pred", {"a": 1}, step=5)
        tracker.log_value("flag", True)

        pred, flag = _read_events(tmp_path)

        assert pred.observation == {"a": 1}
        assert pred.value is None
        assert pred.step == 5
        assert flag.value is True
        assert flag.observation is None

    def test_events_carry_job_identity(self, job_tracker, monkeypatch):
        tracker, tmp_path = job_tracker
        tracker.log_param("model", "mlp")

        [event] = _read_events(tmp_path)

        assert event.tag == "manual_param"
        assert str(event.trial_id) == os.environ["JERNERICS_TRIAL_ID"]

    def test_missing_identity_env_raises(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))
        monkeypatch.setenv("JERNERICS_TRACKING_DIR", str(tmp_path))
        monkeypatch.setenv("JERNERICS_TRIAL_NUMBER", "7")
        monkeypatch.delenv("JERNERICS_TRIAL_ID", raising=False)

        with pytest.raises(RuntimeError, match="JERNERICS_TRIAL_ID"):
            trial_tracker()

    def test_malformed_identity_env_raises(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))
        monkeypatch.setenv("JERNERICS_TRACKING_DIR", str(tmp_path))
        monkeypatch.setenv("JERNERICS_TRIAL_NUMBER", "7")
        monkeypatch.setenv("JERNERICS_TRIAL_ID", "not-a-uuid")
        monkeypatch.setenv("JERNERICS_EXECUTION_ID", str(uuid.uuid4()))

        with pytest.raises(RuntimeError, match="UUID"):
            trial_tracker()

    def test_logs_artifact_event_and_manifest(self, job_tracker):
        tracker, tmp_path = job_tracker
        artifact = tmp_path / "model.pt"
        artifact.write_bytes(b"weights")
        tracker.log_artifact("model", str(artifact))

        [event] = _read_events(tmp_path)
        assert event.tag == "artifact_declaration"
        assert event.key == "model"
        assert event.filename == "model.pt"
        assert event.size_bytes == 7

        manifest = tmp_path / "tracking" / "artifacts" / "7.manifest"
        entries = [json.loads(line) for line in manifest.read_text().splitlines()]
        assert entries == [
            {
                "artifact_id": event.artifact_id.hex,
                "key": "model",
                "path": str(artifact),
            }
        ]

    def test_finish_records_results_and_closes_tracking(self, job_tracker):
        tracker, tmp_path = job_tracker
        tracker.finish({"score": 1.0})

        [event] = _read_events(tmp_path)
        assert event.tag == "value"
        assert event.key == "results"
        assert event.observation == {"score": 1.0}

        with pytest.raises(ValueError):
            tracker.log_value("late", 1.0)


class TestConsoleTracker:
    def test_log_param_prints_param(self, capsys):
        ConsoleTracker().log_param("model", "mlp")

        assert capsys.readouterr().out == "param: model=mlp\n"

    def test_log_value_without_step(self, capsys):
        ConsoleTracker().log_value("loss", 0.5)

        assert capsys.readouterr().out == "[value] loss=0.5\n"

    def test_log_value_with_step(self, capsys):
        ConsoleTracker().log_value("loss", 0.5, step=2)

        assert capsys.readouterr().out == "[step 2] loss=0.5\n"

    def test_log_artifact_prints_artifact(self, capsys):
        ConsoleTracker().log_artifact("model", "/tmp/model.pt")

        assert capsys.readouterr().out == "[artifact] model=/tmp/model.pt\n"

    def test_finish_prints_results(self, capsys):
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
