import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

import optuna
import pytest
from jernerics.runner import (
    _read_trial_results,
    _trial_env,
    _write_trial_config,
    run_trial,
)
from jernerics.tracking.jsonl_io import TrackingWriter
from jernerics_schema import (
    ArtifactDeclarationEvent,
    ValueEvent,
    sweep_id_for,
)
from optuna.storages.journal import JournalFileBackend, JournalStorage

trial_id = uuid4()
execution_id = uuid4()

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

    def test_param_overrides_reach_trial_config(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 1.0})\n')
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
            param_overrides={"target": 3200},
        )

        resolved = json.loads((tmp_path / "configs" / "trial_0.json").read_text())
        assert resolved["target"] == 3200
        assert isinstance(resolved["target"], int)
        assert resolved["lr"] == pytest.approx(0.01)

    def test_param_overrides_override_base_keys(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 1.0})\n')
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
            param_overrides={"lr": 0.5},
        )

        resolved = json.loads((tmp_path / "configs" / "trial_0.json").read_text())
        assert resolved["lr"] == pytest.approx(0.5)

    def test_param_overrides_conflicting_with_search_space_raises(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text("pass\n")
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "n_trials = 1\n"
            "def search_space(trial):\n"
            "    return {'lr': trial.suggest_float('lr', 0.001, 0.1)}\n"
            "backend_overrides = {}\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = tmp_path / "tracking" / "s"
        tracking_dir.mkdir(parents=True)

        with pytest.raises(ValueError, match="lr"):
            run_trial(
                trial_file=str(trial_file),
                config_file=str(config_file),
                study_name="s",
                storage_url=storage_url,
                tracking_dir=str(tracking_dir),
                project_name="proj",
                param_overrides={"lr": 0.1},
            )


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
        sweep_id = sweep_id_for("proj", "study")

        env = _trial_env(
            config_path=config_path,
            tracking_dir="/t",
            project_name="proj",
            study_name="study",
            trial_number=7,
            sweep_id=sweep_id,
            trial_id=trial_id,
            execution_id=execution_id,
        )

        assert env["JERNERICS_TRIAL_CONFIG"] == str(config_path)
        assert env["JERNERICS_TRACKING_DIR"] == "/t"
        assert env["JERNERICS_PROJECT_NAME"] == "proj"
        assert env["JERNERICS_STUDY_NAME"] == "study"
        assert env["JERNERICS_TRIAL_NUMBER"] == "7"
        assert env["JERNERICS_SWEEP_ID"] == str(sweep_id)
        assert env["JERNERICS_TRIAL_ID"] == str(trial_id)
        assert env["JERNERICS_EXECUTION_ID"] == str(execution_id)
        assert "JERNERICS_RUN_ID" not in env
        assert env["KEEP_ME"] == "yes"


def _read_events(path):
    from jernerics.tracking.jsonl_io import TrackingReader

    with TrackingReader(path) as reader:
        return list(reader)


def _first(events, tag, key=None):
    for index, event in enumerate(events):
        if event.tag == tag and (key is None or getattr(event, "key", None) == key):
            return index, event
    raise AssertionError(f"no {tag}{'/' + key if key else ''} event found")


class TestExecutionLifecycleEvents:
    def _tracking_dir(self, tmp_path):
        tracking_dir = tmp_path / "tracking" / "s"
        tracking_dir.mkdir(parents=True)
        return tracking_dir

    def test_success_orders_events_and_end_after_commit(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(
            _HEADER
            + "import time\n"
            + "time.sleep(0.2)\n"
            + 'tracker.log_value("loss", 0.5)\n'
            + 'tracker.finish({"loss": 0.5})\n'
        )
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {'lr': 0.01}\n"
            "n_trials = 1\n"
            "def search_space(trial):\n"
            "    return {'seed': trial.suggest_int('seed', 1, 5)}\n"
            "def objective(results):\n    return results['loss']\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
            heartbeat_interval_s=0.05,
        )

        events = _read_events(tracking_dir / "events" / "0.jsonl")
        start_at, _ = _first(events, "execution_start")
        resolved_at, resolved = _first(events, "value", "resolved_config")
        ask_at, ask_snapshot = _first(events, "trial_snapshot")
        child_at, _ = _first(events, "value", "loss")
        tell_snapshots = [event for event in events if event.tag == "trial_snapshot"]
        end_at, end = _first(events, "execution_end")

        assert start_at < resolved_at < ask_at < child_at < end_at == len(events) - 1
        assert len(tell_snapshots) == 2
        assert ask_snapshot.state.value == "running"
        assert ask_snapshot.objective is None
        terminal = tell_snapshots[1]
        assert terminal.state.value == "completed"
        assert terminal.objective == pytest.approx(0.5)
        assert resolved.observation["lr"] == pytest.approx(0.01)
        assert resolved.observation["seed"] == ask_snapshot.params.root["seed"]
        for snapshot in tell_snapshots:
            assert snapshot.retry_root_trial_id == snapshot.trial_id
            assert snapshot.retry_of_trial_id is None
            assert snapshot.retry_index == 0
        assert any(event.tag == "execution_heartbeat" for event in events)
        assert end.outcome.value == "success"
        assert end.failure_summary is None

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        assert study.trials[0].state == optuna.trial.TrialState.COMPLETE
        assert study.trials[0].value == pytest.approx(0.5)

    def test_nonzero_child_records_failure_and_exits_nonzero(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text("import sys; sys.exit(2)\n")
        config_file = _config_file(tmp_path, objective=False)
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

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

        events = _read_events(tracking_dir / "events" / "0.jsonl")
        snapshots = [event for event in events if event.tag == "trial_snapshot"]
        assert [s.state.value for s in snapshots] == ["running", "failed"]
        assert all(s.objective is None for s in snapshots)
        _, end = _first(events, "execution_end")
        assert end.outcome.value == "failure"
        assert end.exit_code == 2
        assert end.failure_kind is not None
        assert end.failure_kind.value in {"unknown", "nonzero_exit"}
        assert end.failure_summary is not None
        assert len(end.failure_summary) <= 2000
        assert "code 2" in end.failure_summary

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        assert study.trials[0].state == optuna.trial.TrialState.FAIL
        assert events[-1].tag == "execution_end"

    def test_objective_failure_records_exception_kind(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 0.5})\n')
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "n_trials = 1\n"
            "def objective(results):\n    raise ValueError('objective blew up')\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        with pytest.raises(ValueError, match="objective blew up"):
            run_trial(
                trial_file=str(trial_file),
                config_file=str(config_file),
                study_name="s",
                storage_url=storage_url,
                tracking_dir=str(tracking_dir),
                project_name="proj",
            )

        events = _read_events(tracking_dir / "events" / "0.jsonl")
        snapshots = [event for event in events if event.tag == "trial_snapshot"]
        assert [s.state.value for s in snapshots] == ["running", "failed"]
        _, end = _first(events, "execution_end")
        assert end.outcome.value == "failure"
        assert end.failure_kind.value == "exception"
        assert "objective blew up" in end.failure_summary

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        assert study.trials[0].state == optuna.trial.TrialState.FAIL

    def test_commit_failure_records_failure_not_success(self, tmp_path, monkeypatch):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 0.5})\n')
        config_file = _config_file(tmp_path)
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        class FakeTrial:
            number = 0
            params: dict = {}
            distributions: dict = {}
            user_attrs: dict = {}

            def set_user_attr(self, key, value):
                self.user_attrs[key] = value

        class FailingCommitStudy:
            def ask(self):
                return FakeTrial()

            def tell(self, trial, value=None, state=None):
                raise RuntimeError("journal storage write failed")

        monkeypatch.setattr(
            "jernerics.runner.optuna.load_study", lambda *a, **k: FailingCommitStudy()
        )

        with pytest.raises(RuntimeError, match="journal storage write failed"):
            run_trial(
                trial_file=str(trial_file),
                config_file=str(config_file),
                study_name="s",
                storage_url=storage_url,
                tracking_dir=str(tracking_dir),
                project_name="proj",
            )

        _, end = _first(
            _read_events(tracking_dir / "events" / "0.jsonl"), "execution_end"
        )
        assert end.outcome.value == "failure"
        assert end.failure_summary is not None
        assert "journal storage write failed" in end.failure_summary

    def test_signal_terminated_child_records_cancellation(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(
            "import os, signal, time\n"
            "time.sleep(0.1)\n"
            "os.kill(os.getpid(), signal.SIGTERM)\n"
        )
        config_file = _config_file(tmp_path, objective=False)
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

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

        _, end = _first(
            _read_events(tracking_dir / "events" / "0.jsonl"), "execution_end"
        )
        assert end.outcome.value == "cancelled"
        assert end.exit_code is not None
        assert end.exit_code < 0

    def test_snapshots_mirror_all_distribution_kinds(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 0.5})\n')
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "n_trials = 1\n"
            "def search_space(trial):\n"
            "    return {\n"
            "        'rate': trial.suggest_float('rate', 0.0, 1.0),\n"
            "        'seed': trial.suggest_int('seed', 1, 5),\n"
            "        'mode': trial.suggest_categorical('mode', ['a', 'b']),\n"
            "    }\n"
            "def objective(results):\n    return results['loss']\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        _, snapshot = _first(
            _read_events(tracking_dir / "events" / "0.jsonl"), "trial_snapshot"
        )
        distributions = snapshot.distributions.root
        assert set(distributions) == {"rate", "seed", "mode"}
        assert '"FloatDistribution"' in distributions["rate"]
        assert '"IntDistribution"' in distributions["seed"]
        assert '"CategoricalDistribution"' in distributions["mode"]
        for encoded in distributions.values():
            json.loads(encoded)
        assert set(snapshot.params.root) == {"rate", "seed", "mode"}
        assert snapshot.params.root["mode"] in ("a", "b")
        assert isinstance(snapshot.params.root["rate"], float)
        assert isinstance(snapshot.params.root["seed"], int)

    def test_enqueued_lineage_attrs_flow_into_snapshots(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 0.5})\n')
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "n_trials = 2\n"
            "def search_space(trial):\n"
            "    return {'seed': trial.suggest_int('seed', 1, 5)}\n"
            "def objective(results):\n    return results['loss']\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        parent_id = uuid4()
        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        study.enqueue_trial(
            {"seed": 3},
            user_attrs={
                "retry_of": 0,
                "retry_root": 0,
                "retry_index": 1,
                "retry_of_trial_id": str(parent_id),
                "retry_root_trial_id": str(parent_id),
            },
        )

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        events = _read_events(tracking_dir / "events" / "0.jsonl")
        snapshots = [event for event in events if event.tag == "trial_snapshot"]
        for snapshot in snapshots:
            assert snapshot.retry_of_trial_id == parent_id
            assert snapshot.retry_root_trial_id == parent_id
            assert snapshot.retry_index == 1
            assert snapshot.params.root["seed"] == 3
            assert snapshot.attrs.root["retry_index"] == 1
            assert snapshot.attrs.root["retry_root"] == 0
        study_after = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        assert study_after.trials[0].user_attrs["retry_of_trial_id"] == str(parent_id)

    def test_enqueued_lineage_without_root_id_uses_own_identity(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 0.5})\n')
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "n_trials = 2\n"
            "def search_space(trial):\n"
            "    return {'seed': trial.suggest_int('seed', 1, 5)}\n"
            "def objective(results):\n    return results['loss']\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        study.enqueue_trial(
            {"seed": 4},
            user_attrs={
                "retry_of": 5,
                "retry_root": 0,
                "retry_index": 2,
                "retry_of_trial_id": str(uuid4()),
            },
        )

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        events = _read_events(tracking_dir / "events" / "0.jsonl")
        snapshot = next(e for e in events if e.tag == "trial_snapshot")
        assert snapshot.retry_index == 2
        assert snapshot.retry_root_trial_id == snapshot.trial_id
        assert snapshot.retry_of_trial_id is not None

    def test_lineage_without_recorded_parent_id_is_never_invented(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 0.5})\n')
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "n_trials = 2\n"
            "def search_space(trial):\n"
            "    return {'seed': trial.suggest_int('seed', 1, 5)}\n"
            "def objective(results):\n    return results['loss']\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        study.enqueue_trial(
            {"seed": 5},
            user_attrs={"retry_of": 3, "retry_root": 1, "retry_index": 1},
        )

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        events = _read_events(tracking_dir / "events" / "0.jsonl")
        snapshot = next(e for e in events if e.tag == "trial_snapshot")
        assert snapshot.retry_of_trial_id is None
        assert snapshot.retry_root_trial_id == snapshot.trial_id
        assert snapshot.retry_index == 1
        assert snapshot.attrs.root["retry_of"] == 3


    def test_grid_config_carries_enqueued_values_into_trial_config(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + "tracker.finish({'loss': config['lr']})\n")
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {'seed': 1}\n"
            "grid = {'lr': [0.1, 0.2], 'mode': ['a', 'b']}\n"
            "def objective(results):\n    return results['loss']\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        study.enqueue_trial({"lr": 0.2, "mode": "b"})

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        resolved = json.loads((tmp_path / "configs" / "trial_0.json").read_text())
        assert resolved["lr"] == 0.2
        assert resolved["mode"] == "b"
        assert resolved["seed"] == 1
        assert resolved["config_index"] == 0
        study_after = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        assert study_after.trials[0].params == {"lr": 0.2, "mode": "b"}

    def test_grid_duplicate_choices_are_deduplicated_for_suggestions(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + "tracker.finish({'loss': 0.0})\n")
        config_file = tmp_path / "config.py"
        config_file.write_text(
            "base = {}\n"
            "grid = {'mode': ['a', 'a', 'b']}\n"
            "def objective(results):\n    return results['loss']\n"
        )
        storage_url = _make_study(tmp_path)
        tracking_dir = self._tracking_dir(tmp_path)

        study = optuna.load_study(
            study_name="s",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        study.enqueue_trial({"mode": "a"})

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )

        resolved = json.loads((tmp_path / "configs" / "trial_0.json").read_text())
        assert resolved["mode"] == "a"


class TestRunIdRemoval:
    def test_no_run_id_identity_in_trial_context(self):
        from jernerics import trial_context

        assert not [name for name in vars(trial_context) if "RUN_ID" in name]


def _value_event(key: str, *, value=None, observation=None) -> ValueEvent:
    return ValueEvent(
        event_id=uuid4(),
        recorded_at=datetime.now(timezone.utc),
        trial_id=uuid4(),
        key=key,
        step=0,
        value=value,
        observation=observation,
    )


class TestReadTrialResults:
    def test_reads_results_observation(self, tmp_path):
        events = tmp_path / "0.jsonl"
        with TrackingWriter(events) as writer:
            writer.write_event(_value_event("loss", value=0.5))
            writer.write_event(_value_event("results", observation={"loss": 0.5}))

        assert _read_trial_results(events) == {"loss": 0.5}

    def test_reads_results_from_json_string_value(self, tmp_path):
        events = tmp_path / "0.jsonl"
        with TrackingWriter(events) as writer:
            writer.write_event(_value_event("results", value='{"loss": 0.5}'))

        assert _read_trial_results(events) == {"loss": 0.5}

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_trial_results(tmp_path / "missing.jsonl") == {}

    def test_no_results_event_returns_empty(self, tmp_path):
        events = tmp_path / "0.jsonl"
        with TrackingWriter(events) as writer:
            writer.write_event(_value_event("loss", value=0.5))

        assert _read_trial_results(events) == {}

    def test_scalar_results_value_returns_empty(self, tmp_path):
        events = tmp_path / "0.jsonl"
        with TrackingWriter(events) as writer:
            writer.write_event(_value_event("results", value=0.5))

        assert _read_trial_results(events) == {}


class TestSystemLogArtifacts:
    def _run(self, tmp_path, trial_body):
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(trial_body)
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
        return tracking_dir

    def test_child_logs_declared_as_system_artifacts_bound_to_execution(self, tmp_path):
        tracking_dir = self._run(
            tmp_path,
            _HEADER
            + "import sys\n"
            + 'print("child-out")\n'
            + 'print("child-err", file=sys.stderr)\n'
            + 'tracker.finish({"loss": 0.5})\n',
        )

        events = _read_events(tracking_dir / "events" / "0.jsonl")
        execution = _first(events, "execution_start")[1]
        declarations = [
            event for event in events if isinstance(event, ArtifactDeclarationEvent)
        ]
        by_key = {event.key: event for event in declarations}

        assert set(by_key) == {"stdout", "stderr"}
        stdout_file = tracking_dir / "logs" / "trial-0.stdout"
        stderr_file = tracking_dir / "logs" / "trial-0.stderr"
        assert stdout_file.read_text() == "child-out\n"
        assert stderr_file.read_text() == "child-err\n"
        for key, path in (("stdout", stdout_file), ("stderr", stderr_file)):
            event = by_key[key]
            assert event.source == "system"
            assert event.filename == path.name
            assert event.execution_id == execution.execution_id
            payload = path.read_bytes()
            assert event.size_bytes == len(payload)
            assert event.sha256 == hashlib.sha256(payload).hexdigest()

        manifest_lines = (
            (tracking_dir / "artifacts" / "0.manifest").read_text().splitlines()
        )
        assert {json.loads(line)["artifact_id"] for line in manifest_lines} == {
            event.artifact_id.hex for event in declarations
        }

    def test_failed_child_still_declares_its_logs(self, tmp_path):
        with pytest.raises(SystemExit):
            self._run(tmp_path, "import sys; sys.exit(3)\n")

        events = _read_events(tmp_path / "tracking" / "s" / "events" / "0.jsonl")
        declarations = [
            event for event in events if isinstance(event, ArtifactDeclarationEvent)
        ]
        assert {event.key for event in declarations} == {"stdout", "stderr"}

    def test_no_tracking_dir_means_no_log_capture(self, tmp_path, capfd, monkeypatch):
        monkeypatch.chdir(tmp_path)
        trial_file = tmp_path / "trial.py"
        trial_file.write_text('print("passthrough")\n')
        config_file = _config_file(tmp_path, objective=False)
        storage_url = _make_study(tmp_path)

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="s",
            storage_url=storage_url,
            project_name="proj",
        )

        assert "passthrough" in capfd.readouterr().out
