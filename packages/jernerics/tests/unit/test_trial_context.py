import io
import json
import os
import uuid
from unittest.mock import MagicMock

import pytest
from jernerics.tracking.jsonl_io import TrackingReader
from jernerics.trial_context import (
    ConsoleTracker,
    TrackerProtocol,
    _JobTracker,
    is_job,
    trial_config,
    trial_tracker,
)
from jernerics_schema import JSON_VALUE_MAX_BYTES, ArtifactDeclarationEvent
from pydantic import ValidationError


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

    def test_log_metric_delegates_to_log_value(self):
        tracker = MagicMock()
        _JobTracker(tracker).log_metric("loss", 0.5, step=3)

        tracker.log_value.assert_called_once_with("loss", 0.5, step=3)

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

        staged = (
            tmp_path
            / "tracking"
            / "artifacts"
            / "blobs"
            / f"{event.artifact_id.hex}.bin"
        )
        assert staged.read_bytes() == b"weights"
        manifest = tmp_path / "tracking" / "artifacts" / "7.manifest"
        entries = [json.loads(line) for line in manifest.read_text().splitlines()]
        assert entries == [
            {
                "artifact_id": event.artifact_id.hex,
                "key": "model",
                "path": str(staged),
                "staged": True,
            }
        ]

    def test_open_artifact_streams_into_staged_blob(self, job_tracker):
        tracker, tmp_path = job_tracker

        with tracker.open_artifact("report", "wt") as f:
            assert isinstance(f, io.TextIOBase)
            f.write("hello")

        [event] = _read_events(tmp_path)
        assert isinstance(event, ArtifactDeclarationEvent)
        assert event.key == "report"
        assert event.filename == "report"
        assert event.size_bytes == 5
        staged = (
            tmp_path
            / "tracking"
            / "artifacts"
            / "blobs"
            / f"{event.artifact_id.hex}.bin"
        )
        assert staged.read_bytes() == b"hello"
        manifest = tmp_path / "tracking" / "artifacts" / "7.manifest"
        entries = [json.loads(line) for line in manifest.read_text().splitlines()]
        assert entries == [
            {
                "artifact_id": event.artifact_id.hex,
                "key": "report",
                "path": str(staged),
                "staged": True,
            }
        ]

    def test_open_artifact_rejects_bad_mode_eagerly(self, job_tracker):
        tracker, tmp_path = job_tracker

        with pytest.raises(ValueError, match=r"mode must be 'wt' or 'wb'"):
            tracker.open_artifact("m", "bx")

        assert not (tmp_path / "tracking" / "artifacts" / "blobs").exists()

    def test_oversize_finish_names_artifact_remedy(self, job_tracker):
        tracker, _ = job_tracker

        with pytest.raises(
            ValidationError,
            match=r"with tracker\.open_artifact\(key, 'wt'\)",
        ):
            tracker.finish({"pad": "x" * (JSON_VALUE_MAX_BYTES + 1)})

    def test_set_progress_emits_execution_progress_event(self, job_tracker):
        tracker, tmp_path = job_tracker
        tracker.set_progress(4, 10, "epochs")

        [event] = _read_events(tmp_path)

        assert event.tag == "execution_progress"
        assert event.current == 4
        assert event.total == 10
        assert event.unit == "epochs"

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

    def test_log_metric_prints_same_output_as_log_value(self, capsys):
        ConsoleTracker().log_value("loss", 0.5, step=2)
        expected = capsys.readouterr().out

        ConsoleTracker().log_metric("loss", 0.5, step=2)

        assert capsys.readouterr().out == expected

    def test_log_artifact_prints_artifact(self, capsys):
        ConsoleTracker().log_artifact("model", "/tmp/model.pt")

        assert capsys.readouterr().out == "[artifact] model=/tmp/model.pt\n"

    def test_open_artifact_prints_size_after_clean_exit(self, capsys):
        with ConsoleTracker().open_artifact("model", "wt") as f:
            assert isinstance(f, io.TextIOBase)
            f.write("abc")

        assert capsys.readouterr().out == "[artifact] model=3 bytes\n"

    def test_open_artifact_prints_nothing_on_exception(self, capsys):
        with (
            pytest.raises(RuntimeError, match="boom"),
            ConsoleTracker().open_artifact("model", "wt") as f,
        ):
            assert isinstance(f, io.TextIOBase)
            raise RuntimeError("boom")

        assert capsys.readouterr().out == ""

    def test_open_artifact_rejects_bad_mode(self):
        with pytest.raises(ValueError, match=r"mode must be 'wt' or 'wb'"):
            ConsoleTracker().open_artifact("model", "bx")

    def test_set_progress_prints_progress(self, capsys):
        ConsoleTracker().set_progress(3, 10, "epochs")

        assert capsys.readouterr().out == "[progress] 3/10 epochs\n"

    def test_finish_prints_results(self, capsys):
        ConsoleTracker().finish({"score": 0.9, "status": "ok"})

        assert capsys.readouterr().out == "results:\n  score=0.9\n  status=ok\n"


class TestPublicSurface:
    def test_protocol_exposes_log_metric(self):
        assert hasattr(TrackerProtocol, "log_metric")

    @pytest.mark.parametrize("method", ["log_text", "log_json", "log_sweep_meta"])
    def test_console_lacks_backend_methods(self, method):
        assert not hasattr(ConsoleTracker(), method)

    @pytest.mark.parametrize("method", ["log_text", "log_json", "log_sweep_meta"])
    def test_job_tracker_lacks_backend_methods(self, job_tracker, method):
        tracker, _ = job_tracker
        assert not hasattr(tracker, method)
