import optuna
import pytest
from jernerics.backend.local_backend import LocalBackend
from jernerics.backend.models import SweepSubmission
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
