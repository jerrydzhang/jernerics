from unittest.mock import MagicMock, patch

import pytest
from jernerics.backend.adapter import SchedulerAdapter
from jernerics.backend.local_backend import LocalBackend
from jernerics.backend.pueue.adapter import PUEUE_OVERRIDE_KEYS, PueueAdapter
from jernerics.backend.slurm.adapter import SBATCH_OVERRIDE_KEYS, SlurmAdapter
from jernerics.cli import app
from jernerics.config import ExitCode
from typer.testing import CliRunner

runner = CliRunner()


def _slurm_adapter():
    return SlurmAdapter(host=MagicMock(), remote_dir="/scratch/proj")


def _pueue_adapter():
    return PueueAdapter(
        host=MagicMock(),
        remote_dir="/home/u/proj",
        cache_dir="/home/u/.cache/jernerics",
    )


def _make_backend(adapter):
    backend = MagicMock()
    backend.adapter = adapter
    backend.tracking_server = None
    backend.storage_path.return_value = "/storage/study.journal"
    backend.prepare_and_submit.return_value = None
    return backend


@pytest.fixture
def run_project(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test-project"\nversion = "0.1.0"\n'
    )
    (tmp_path / "trial.py").write_text("config = {}\n")
    (tmp_path / "config.py").write_text("base = {}\nbackend_overrides = {}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "jernerics.commands.execution._capture_git_hash", lambda _: None
    )
    monkeypatch.setattr("jernerics.commands.execution.cache_dir", lambda: tmp_path)
    return tmp_path


def _invoke(run_project, backend, *opts):
    with patch(
        "jernerics.commands.execution._get_backend",
        return_value=(backend, "test-project", run_project),
    ):
        return runner.invoke(
            app,
            [
                "run",
                str(run_project / "trial.py"),
                str(run_project / "config.py"),
                "--backend",
                "target",
                *opts,
            ],
        )


class TestAdapterOverrideKeyContract:
    def test_slurm_surfaces_sbatch_keys(self):
        assert _slurm_adapter().valid_override_keys() == SBATCH_OVERRIDE_KEYS

    def test_pueue_consumes_only_max_parallel(self):
        assert _pueue_adapter().valid_override_keys() == PUEUE_OVERRIDE_KEYS
        assert frozenset({"max_parallel"}) == PUEUE_OVERRIDE_KEYS

    def test_adapters_satisfy_protocol(self):
        assert isinstance(_slurm_adapter(), SchedulerAdapter)
        assert isinstance(_pueue_adapter(), SchedulerAdapter)


class TestRunRemoteOverrideValidation:
    def test_slurm_accepts_sbatch_key(self, run_project):
        backend = _make_backend(_slurm_adapter())

        result = _invoke(run_project, backend, "--set", "partition=debug")

        assert result.exit_code == ExitCode.SUCCESS
        backend.prepare_and_submit.assert_called_once()
        assert backend.prepare_and_submit.call_args.kwargs["cli_overrides"] == {
            "partition": "debug"
        }

    def test_slurm_rejects_unknown_key(self, run_project):
        backend = _make_backend(_slurm_adapter())

        result = _invoke(run_project, backend, "--set", "bogus=1")

        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "Unknown override key(s) for backend 'target': bogus" in result.output
        assert "partition" in result.output
        backend.prepare_and_submit.assert_not_called()

    def test_pueue_accepts_max_parallel(self, run_project):
        backend = _make_backend(_pueue_adapter())

        result = _invoke(run_project, backend, "--set", "max_parallel=2")

        assert result.exit_code == ExitCode.SUCCESS
        backend.prepare_and_submit.assert_called_once()
        assert backend.prepare_and_submit.call_args.kwargs["cli_overrides"] == {
            "max_parallel": "2"
        }

    def test_pueue_rejects_sbatch_only_key(self, run_project):
        backend = _make_backend(_pueue_adapter())

        result = _invoke(run_project, backend, "--set", "partition=debug")

        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert (
            "Unknown override key(s) for backend 'target': partition" in result.output
        )
        assert "Valid keys: max_parallel" in result.output
        backend.prepare_and_submit.assert_not_called()

    def test_pueue_rejects_mixed_known_and_unknown(self, run_project):
        backend = _make_backend(_pueue_adapter())

        result = _invoke(
            run_project, backend, "--set", "max_parallel=2", "--set", "mem=4G"
        )

        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "Unknown override key(s) for backend 'target': mem" in result.output
        backend.prepare_and_submit.assert_not_called()


class TestLocalStaysLenient:
    def test_local_backend_outside_adapter_contract(self):
        backend = LocalBackend()

        assert not isinstance(backend, SchedulerAdapter)
        assert not hasattr(backend, "valid_override_keys")

    def test_run_local_performs_no_override_validation(self, run_project):
        with patch("jernerics.commands.execution.LocalBackend") as local_backend_cls:
            result = runner.invoke(
                app,
                [
                    "local",
                    str(run_project / "trial.py"),
                    str(run_project / "config.py"),
                    "--set",
                    "partition=debug",
                ],
            )

        assert "Unknown override key(s)" not in result.output
        local_backend_cls.return_value.submit_sweep.assert_not_called()


class TestSetParamPathUntouched:
    def test_set_param_reaches_spec_without_key_validation(self, run_project):
        backend = _make_backend(_pueue_adapter())

        result = _invoke(run_project, backend, "--set-param", "learning_rate=0.5")

        assert result.exit_code == ExitCode.SUCCESS
        spec = backend.prepare_and_submit.call_args.args[0]
        assert spec.param_overrides == {"learning_rate": 0.5}

    def test_set_param_arbitrary_keys_not_rejected(self, run_project):
        backend = _make_backend(_slurm_adapter())

        result = _invoke(run_project, backend, "--set-param", "anything=1")

        assert result.exit_code == ExitCode.SUCCESS
        spec = backend.prepare_and_submit.call_args.args[0]
        assert spec.param_overrides == {"anything": 1}

    def test_set_and_set_param_combine_on_pueue(self, run_project):
        backend = _make_backend(_pueue_adapter())

        result = _invoke(
            run_project, backend, "--set", "max_parallel=3", "--set-param", "lr=0.1"
        )

        assert result.exit_code == ExitCode.SUCCESS
        call = backend.prepare_and_submit.call_args
        assert call.kwargs["cli_overrides"] == {"max_parallel": "3"}
        assert call.args[0].param_overrides == {"lr": 0.1}
