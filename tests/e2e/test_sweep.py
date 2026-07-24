import sqlite3

import optuna
import pytest
from jernerics.backend.local_backend import LocalBackend
from jernerics.backend.models import SweepSubmission
from jernerics.tracking.batch_sync import replay_tracking, sync_artifacts
from jernerics.tracking.infra import resolve_artifact_storage
from optuna.storages.journal import JournalFileBackend, JournalStorage

STUDY_NAME = "test-sweep"


def _write_trial(path, body):
    """Write a minimal trial file defining ``trial(config, tracker)``."""
    path.write_text(f"def trial(config, tracker):\n    {body}\n")


def _write_config(path, base=None, n_trials=2, objective_expr=None):
    lines = [f"base = {base or {'lr': 0.01}}"]
    lines.append(f"n_trials = {n_trials}")
    if objective_expr:
        lines.append(f"def objective(results):\n    return {objective_expr}")
    lines.append("backend_overrides = {}")
    path.write_text("\n".join(lines) + "\n")


def _make_spec(tmp_path, trial_file, config_file, n_trials=2):
    journal_dir = tmp_path / "optuna"
    journal_dir.mkdir(exist_ok=True)
    return SweepSubmission(
        trial_path=trial_file,
        config_path=config_file,
        study_name=STUDY_NAME,
        storage_url=str(journal_dir / f"{STUDY_NAME}.journal"),
        n_trials=n_trials,
        tracking_dir=tmp_path / "tracking" / STUDY_NAME,
    )


class TestBasicSweep:
    def test_optuna_study_has_correct_trials_and_objectives(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        config_file = tmp_path / "config.py"

        _write_trial(trial_file, 'return {"loss": config["lr"] * 2}')
        _write_config(config_file, objective_expr='results["loss"]')

        spec = _make_spec(tmp_path, trial_file, config_file, n_trials=2)
        backend = LocalBackend()
        result = backend.submit_sweep(spec)

        assert len(result.submissions) == 1
        assert result.submissions[0].n_trials == 2

        storage = JournalStorage(JournalFileBackend(spec.storage_url))
        study = optuna.load_study(study_name=STUDY_NAME, storage=storage)
        assert len(study.trials) == 2
        assert all(t.state == optuna.trial.TrialState.COMPLETE for t in study.trials)
        assert all(abs(t.value - 0.02) < 1e-6 for t in study.trials)


class TestArtifactRoundTrip:
    def test_artifacts_uploaded_and_served_over_http(self, tmp_path, http_server):
        base_url, _db_path = http_server

        artifact_dir = tmp_path / "artifact_data"
        artifact_dir.mkdir()

        trial_file = tmp_path / "trial.py"
        config_file = tmp_path / "config.py"

        _write_trial(
            trial_file,
            "import os\n"
            '    path = os.path.join(config["artifact_dir"], "model.txt")\n'
            '    with open(path, "w") as f:\n'
            '        f.write("model data")\n'
            "    tracker.log_artifact('model', path)\n"
            '    return {"loss": config["lr"] * 2}',
        )
        _write_config(
            config_file,
            base={"lr": 0.01, "artifact_dir": str(artifact_dir)},
            n_trials=2,
            objective_expr='results["loss"]',
        )

        spec = _make_spec(tmp_path, trial_file, config_file, n_trials=2)
        backend = LocalBackend()
        backend.submit_sweep(spec)

        upload_fn = resolve_artifact_storage(base_url)
        assert upload_fn is not None
        sync_artifacts(
            tracking_dir=tmp_path / "tracking",
            upload_fn=upload_fn,
            project="test-project",
            study=STUDY_NAME,
        )

        import httpx

        for trial_id in (0, 1):
            key = f"test-project/{STUDY_NAME}/{trial_id}/model"
            resp = httpx.get(f"{base_url}/artifact/{key}")
            assert resp.status_code == 200
            assert resp.text == "model data"


class TestTrackingReplayRoundTrip:
    def test_replay_sends_all_event_types_to_sqlite(self, tmp_path, http_server):
        base_url, db_path = http_server

        trial_file = tmp_path / "trial.py"
        config_file = tmp_path / "config.py"

        _write_trial(
            trial_file,
            'lr = config["lr"]\n'
            "    tracker.log_param('lr', lr)\n"
            "    tracker.log_value('loss', lr * 2, step=1)\n"
            "    tracker.log_json('summary', {'accuracy': 0.95})\n"
            '    return {"loss": lr * 2}',
        )
        _write_config(config_file, objective_expr='results["loss"]')

        spec = _make_spec(tmp_path, trial_file, config_file, n_trials=2)
        backend = LocalBackend()
        backend.submit_sweep(spec)

        # Replay .jsonl files to HTTP server
        tracking_parent = (tmp_path / "tracking" / STUDY_NAME).parent
        replay_tracking(
            tracking_dir=tracking_parent, base_url=base_url, study=STUDY_NAME
        )

        # Verify all event types in SQLite
        con = sqlite3.connect(str(db_path))
        assert con.execute("SELECT COUNT(*) FROM params").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM tracked_values").fetchone()[0] == 4
        assert con.execute("SELECT COUNT(*) FROM trial_end").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM sweep_meta").fetchone()[0] == 2
        con.close()


class TestSweepFailure:
    def test_failed_trial_raises_runtime_error(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        config_file = tmp_path / "config.py"

        _write_trial(trial_file, 'raise ValueError("boom")')
        _write_config(config_file, n_trials=1)

        spec = _make_spec(tmp_path, trial_file, config_file, n_trials=1)
        backend = LocalBackend()

        with pytest.raises(RuntimeError, match="One or more trials failed"):
            backend.submit_sweep(spec)
