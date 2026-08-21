import httpx
import optuna
import pytest
from jernerics.backend.local_backend import LocalBackend
from jernerics.backend.models import SweepSubmission
from jernerics.tracking.blob_uploader import BlobUploadResult
from jernerics_schema import sweep_id_for
from jernerics_server.store import Store
from optuna.storages.journal import JournalFileBackend, JournalStorage

STUDY_NAME = "test-sweep"

_TRIAL_HEADER = (
    "from jernerics import trial_config, trial_tracker\n"
    "config = trial_config()\n"
    "tracker = trial_tracker()\n"
)


def _write_trial(path, body):
    """Write a trial script executed as a subprocess by the runner."""
    path.write_text(_TRIAL_HEADER + body + "\n")


def _write_config(path, base=None, n_trials=2, objective_expr=None):
    lines = [f"base = {base or {'lr': 0.01}}"]
    lines.append(f"n_trials = {n_trials}")
    if objective_expr:
        lines.append(f"def objective(results):\n    return {objective_expr}")
    lines.append("backend_overrides = {}")
    path.write_text("\n".join(lines) + "\n")


def _make_spec(
    tmp_path,
    trial_file,
    config_file,
    n_trials=2,
    project_name=None,
    server_addr=None,
):
    journal_dir = tmp_path / "optuna"
    journal_dir.mkdir(exist_ok=True)
    return SweepSubmission(
        trial_path=trial_file,
        config_path=config_file,
        study_name=STUDY_NAME,
        storage_url=str(journal_dir / f"{STUDY_NAME}.journal"),
        n_trials=n_trials,
        tracking_dir=tmp_path / "tracking" / STUDY_NAME,
        project_name=project_name,
        server_addr=server_addr,
    )


class TestBasicSweep:
    def test_optuna_study_has_correct_trials_and_objectives(self, tmp_path):
        trial_file = tmp_path / "trial.py"
        config_file = tmp_path / "config.py"

        _write_trial(trial_file, 'tracker.finish({"loss": config["lr"] * 2})')
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


class TestTerminalSweepState:
    def test_completed_sweep_reports_terminal_state_and_blob(
        self, tmp_path, http_server, monkeypatch
    ):
        base_url, db_path = http_server
        trial_file = tmp_path / "trial.py"
        config_file = tmp_path / "config.py"

        _write_trial(
            trial_file,
            "import os\n"
            "from pathlib import Path\n"
            "out = Path(os.environ['JERNERICS_TRACKING_DIR']).parent / "
            "'artifacts-out'\n"
            "out.mkdir(exist_ok=True)\n"
            "checkpoint = out / 'checkpoint.bin'\n"
            "checkpoint.write_bytes(b'checkpoint-bytes')\n"
            "tracker.log_artifact('checkpoint', str(checkpoint))\n"
            'tracker.finish({"loss": 0.5})\n',
        )
        _write_config(config_file, n_trials=1, objective_expr='results["loss"]')

        def strand_blobs(base_url, api_key, manifest_paths, **kwargs):
            return BlobUploadResult(failed=1)

        monkeypatch.setattr(
            "jernerics.tracking.trial_environment.upload_pending_blobs",
            strand_blobs,
        )

        spec = _make_spec(
            tmp_path,
            trial_file,
            config_file,
            n_trials=1,
            project_name="proj",
            server_addr=base_url,
        )
        LocalBackend(tracking_server=base_url).submit_sweep(spec)

        response = httpx.post(
            f"{base_url}/sweeps", json={"selection": {"project": "proj"}}
        )
        records = response.json()["records"]
        assert len(records) == 1
        assert records[0]["name"] == STUDY_NAME
        assert records[0]["sweep_id"] == str(sweep_id_for("proj", STUDY_NAME))
        assert records[0]["state"] == "completed"

        with Store(db_path) as store:
            _, rows = store.query(
                "SELECT received_ns FROM artifacts WHERE key = 'checkpoint'"
            )
        assert rows and rows[0][0] is not None

    def test_failed_sweep_reports_failed_state(self, tmp_path, http_server):
        base_url, _ = http_server
        trial_file = tmp_path / "trial.py"
        config_file = tmp_path / "config.py"

        _write_trial(trial_file, 'raise ValueError("boom")')
        _write_config(config_file, n_trials=1)

        spec = _make_spec(
            tmp_path,
            trial_file,
            config_file,
            n_trials=1,
            project_name="proj",
            server_addr=base_url,
        )

        with pytest.raises(RuntimeError, match="One or more trials failed"):
            LocalBackend(tracking_server=base_url).submit_sweep(spec)

        response = httpx.post(
            f"{base_url}/sweeps", json={"selection": {"project": "proj"}}
        )
        records = response.json()["records"]
        assert len(records) == 1
        assert records[0]["state"] == "failed"
