import json

import optuna
import pytest
from jernerics.runner import (
    _read_trial_results,
    _trial_env,
    _write_trial_config,
    run_trial,
)
from jernerics.tracking.jsonl_io import TrackingWriter
from optuna.storages.journal import JournalFileBackend, JournalStorage

_HEADER = (
    "from jernerics import trial_config, trial_tracker\n"
    "config = trial_config()\n"
    "tracker = trial_tracker()\n"
)


def _make_study(tmp_path, name="s"):
    storage_url = str(tmp_path / f"{name}.journal")
    optuna.create_study(
        study_name=name,
        storage=JournalStorage(JournalFileBackend(storage_url)),
        direction="minimize",
    )
    return storage_url


def _config_file(tmp_path, *, objective=True):
    path = tmp_path / "config.py"
    lines = ["base = {'lr': 0.01}", "n_trials = 1"]
    if objective:
        lines.append("def objective(results):\n    return results['loss']")
    lines.append("backend_overrides = {}")
    path.write_text("\n".join(lines) + "\n")
    return path


class TestRunTrialSubprocess:
    def test_executes_script_reads_results_writes_config(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": config["lr"] * 2})\n')
        config_file = _config_file(tmp_path)
        storage_url = _make_study(tmp_path)
        tracking_dir = tmp_path / "tracking" / "s"
        tracking_dir.mkdir(parents=True)

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        assert len(study.trials) == 1
        assert study.trials[0].state == optuna.trial.TrialState.COMPLETE
        assert study.trials[0].value == pytest.approx(0.02)

        config_json = tmp_path / "configs" / "trial_0.json"
        assert config_json.exists()
        resolved = json.loads(config_json.read_text())
        assert resolved["lr"] == pytest.approx(0.01)
        assert resolved["config_index"] == 0

    def test_failed_trial_exits_nonzero(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text("import sys; sys.exit(2)\n")
        config_file = _config_file(tmp_path, objective=False)
        storage_url = _make_study(tmp_path)
        tracking_dir = tmp_path / "tracking" / "s"
        tracking_dir.mkdir(parents=True)

        with pytest.raises(SystemExit) as exc_info:
            run_trial(
                trial_file=str(trial_file),
                config_file=str(config_file),
                study_name="s",
                storage_url=storage_url,
                tracking_dir=str(tracking_dir),
                project_name="proj",
            )
        assert exc_info.value.code == 1

    def test_objective_none_returns_zero(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 1.0})\n')
        config_file = _config_file(tmp_path, objective=False)
        storage_url = _make_study(tmp_path)
        tracking_dir = tmp_path / "tracking" / "s"
        tracking_dir.mkdir(parents=True)

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        assert study.trials[0].value == pytest.approx(0.0)


class TestWriteTrialConfig:
    def test_writes_resolved_config_under_cache_configs(self, tmp_path):
        tracking_dir = tmp_path / "tracking" / "s"
        tracking_dir.mkdir(parents=True)

        path = _write_trial_config({"lr": 0.1, "config_index": 2}, str(tracking_dir), 2)

        assert path == tmp_path / "configs" / "trial_2.json"
        assert json.loads(path.read_text()) == {"lr": 0.1, "config_index": 2}


class TestTrialEnv:
    def test_contains_all_required_jernerics_vars(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEEP_ME", "yes")
        config_path = tmp_path / "c.json"

        env = _trial_env(
            config_path=config_path,
            tracking_dir="/t",
            project_name="proj",
            study_name="study",
            trial_number=7,
            run_id=99,
        )

        assert env["JERNERICS_TRIAL_CONFIG"] == str(config_path)
        assert env["JERNERICS_TRACKING_DIR"] == "/t"
        assert env["JERNERICS_PROJECT_NAME"] == "proj"
        assert env["JERNERICS_STUDY_NAME"] == "study"
        assert env["JERNERICS_TRIAL_NUMBER"] == "7"
        assert env["JERNERICS_RUN_ID"] == "99"
        assert env["KEEP_ME"] == "yes"


class TestReadTrialResults:
    def test_reads_results_envelope(self, tmp_path):
        events = tmp_path / "0.jsonl"
        writer = TrackingWriter(events)
        writer.write_envelope({"value": {"key": "loss", "value": 0.5}})
        writer.write_envelope(
            {"value": {"key": "results", "value_json": '{"loss": 0.5}'}}
        )
        writer.write_envelope({"trial_end": {}})
        writer.close()

        assert _read_trial_results(events) == {"loss": 0.5}

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_trial_results(tmp_path / "missing.jsonl") == {}

    def test_no_results_envelope_returns_empty(self, tmp_path):
        events = tmp_path / "0.jsonl"
        writer = TrackingWriter(events)
        writer.write_envelope({"value": {"key": "loss", "value": 0.5}})
        writer.write_envelope({"trial_end": {}})
        writer.close()

        assert _read_trial_results(events) == {}

    def test_results_envelope_value_must_be_dict(self, tmp_path):
        events = tmp_path / "0.jsonl"
        writer = TrackingWriter(events)
        writer.write_envelope({"value": "not-a-dict"})
        writer.write_envelope(
            {"value": {"key": "results", "value_json": '{"loss": 0.5}'}}
        )
        writer.close()

        assert _read_trial_results(events) == {"loss": 0.5}
