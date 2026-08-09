import json

import pytest
from jernerics.tracking.tracker import JsonlTracker
from jernerics.trial_context import (
    ConsoleTracker,
    is_job,
    trial_config,
    trial_tracker,
)


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
        config_path.write_text(json.dumps({"lr": 0.1, "epochs": 3}))
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))

        assert trial_config() == {"lr": 0.1, "epochs": 3}

    def test_returns_defaults_as_is_in_standalone_mode(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)
        defaults = {"lr": 0.1}

        assert trial_config(defaults) is defaults

    def test_raises_when_standalone_without_defaults(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)

        with pytest.raises(ValueError, match="defaults is required"):
            trial_config()

    def test_raises_when_job_config_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(tmp_path / "missing.json"))

        with pytest.raises(RuntimeError, match="Unable to read trial config"):
            trial_config()


class TestTrialTracker:
    def test_returns_console_tracker_in_standalone_mode(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_TRIAL_CONFIG", raising=False)

        assert isinstance(trial_tracker(), ConsoleTracker)

    def test_returns_tracker_in_job_mode(self, monkeypatch, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text("{}")
        monkeypatch.setenv("JERNERICS_TRIAL_CONFIG", str(config_path))
        monkeypatch.setenv("JERNERICS_TRACKING_DIR", str(tmp_path / "tracking"))
        monkeypatch.setenv("JERNERICS_PROJECT_NAME", "proj")
        monkeypatch.setenv("JERNERICS_STUDY_NAME", "study")
        monkeypatch.setenv("JERNERICS_TRIAL_NUMBER", "7")
        monkeypatch.setenv("JERNERICS_RUN_ID", "123")

        tracker = trial_tracker()

        assert isinstance(tracker, JsonlTracker)
        tracker.finish({"score": 1.0})
        event_path = tmp_path / "tracking" / "events" / "7.jsonl"
        events = [json.loads(line) for line in event_path.read_text().splitlines()]
        assert events[0]["project"] == "proj"
        assert events[0]["study_name"] == "study"
        assert events[0]["trial_id"] == 7
        assert events[0]["run_id"] == 123


class TestConsoleTracker:
    def test_prints_all_methods_to_stdout(self, capsys):
        tracker = ConsoleTracker()

        tracker.log_param("model", "mlp")
        tracker.log_value("loss", 0.25, 3)
        tracker.log_text("note", "ok")

        assert capsys.readouterr().out == (
            "param: model=mlp\n[step 3] loss=0.25\n[text] note=ok\n"
        )

    def test_finish_prints_summary(self, capsys):
        tracker = ConsoleTracker()

        tracker.finish({"score": 0.9, "status": "ok"})

        assert capsys.readouterr().out == "results:\n  score=0.9\n  status=ok\n"

    def test_logs_json_and_artifacts_to_stdout(self, capsys):
        tracker = ConsoleTracker()

        tracker.log_json("pred", {"a": 1}, step=5)
        tracker.log_artifact("model.pt", "/tmp/model.pt")
        tracker.log_value("loss", 0.5, step=5)

        out = capsys.readouterr().out
        assert '[step 5] pred={"a": 1}\n' in out
        assert "[artifact] model.pt=/tmp/model.pt\n" in out
        assert "[step 5] loss=0.5\n" in out
