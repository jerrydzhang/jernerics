from __future__ import annotations

from jernerics._cli_helpers import HpcConfig, MlflowConfig, SweepConfig


def _generate_sweep_script(
    array_spec: str,
    slurm_opts: dict[str, str],
    dag_relpath: str,
    config_relpath: str,
    remote_dir: str,
    hpc_config: HpcConfig,
    binds: dict[str, str],
    project_name: str,
    mlflow_config: MlflowConfig,
    study_name: str,
    sweep: SweepConfig,
) -> str:
    from jernerics.cli import _generate_sweep_script as _gen

    return _gen(
        array_spec=array_spec,
        slurm_opts=slurm_opts,
        dag_relpath=dag_relpath,
        config_relpath=config_relpath,
        remote_dir=remote_dir,
        hpc_config=hpc_config,
        binds=binds,
        project_name=project_name,
        mlflow_config=mlflow_config,
        study_name=study_name,
        sweep=sweep,
    )


def _make_hpc_config(**overrides):
    defaults = {
        "host": "user@hpc.example.edu",
        "remote_dir": "~/experiments/test",
        "partition": "priority",
        "time": "1:00:00",
        "mem": "16G",
        "cpus": 4,
        "max_concurrent_jobs": 10,
        "cache_dir": None,
    }
    defaults.update(overrides)
    return HpcConfig(**defaults)


class TestSweepScriptGeneration:
    def test_sweep_script_contains_optuna(self, tmp_path):
        sweep = SweepConfig(
            _base={"seed": 42},
            search_space=lambda trial: {"lr": trial.suggest_float("lr", 1e-5, 1e-1)},
            n_trials=50,
            sampler=None,
            objective_task="train",
            objective_metric="loss",
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        script = _generate_sweep_script(
            array_spec="1-50%10",
            slurm_opts={"partition": "gpu", "time": "1:00:00", "mem": "16G"},
            dag_relpath="experiments/dag.py",
            config_relpath="experiments/config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="test-project",
            mlflow_config=MlflowConfig(),
            study_name="test-project_config_20260101-120000",
            sweep=sweep,
        )

        assert "import optuna" in script
        assert "study.ask()" in script
        assert "study.tell" in script
        assert "#SBATCH --array=1-50%10" in script

    def test_sweep_script_single_trial(self, tmp_path):
        sweep = SweepConfig(
            _base={"seed": 42},
            search_space=None,
            n_trials=1,
            sampler=None,
            objective_task=None,
            objective_metric=None,
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        script = _generate_sweep_script(
            array_spec="1-1",
            slurm_opts={"partition": "gpu"},
            dag_relpath="dag.py",
            config_relpath="config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="test-project",
            mlflow_config=MlflowConfig(),
            study_name="test-project_config_20260101-120000",
            sweep=sweep,
        )

        assert "#SBATCH --array=1-1" in script
        assert "study.tell(trial, 0.0)" in script

    def test_array_spec_calculation(self):
        from jernerics.cli import DEFAULT_SLURM

        assert "output" in DEFAULT_SLURM

    def test_study_name_format(self, tmp_path):
        sweep = SweepConfig(
            _base={},
            search_space=None,
            n_trials=10,
            sampler=None,
            objective_task=None,
            objective_metric=None,
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        study_name = "myproject_config_20260419-143000"
        script = _generate_sweep_script(
            array_spec="1-10",
            slurm_opts={"partition": "gpu"},
            dag_relpath="dag.py",
            config_relpath="config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="myproject",
            mlflow_config=MlflowConfig(),
            study_name=study_name,
            sweep=sweep,
        )

        assert f"study_name={study_name!r}" in script
        assert "sqlite:///" in script
        assert ".jernerics/optuna/" in script

    def test_mlflow_env_vars_present(self, tmp_path):
        sweep = SweepConfig(
            _base={},
            search_space=None,
            n_trials=5,
            sampler=None,
            objective_task=None,
            objective_metric=None,
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        script = _generate_sweep_script(
            array_spec="1-5",
            slurm_opts={"partition": "gpu"},
            dag_relpath="dag.py",
            config_relpath="config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="test-project",
            mlflow_config=MlflowConfig(
                tracking_uri="http://localhost:5000",
                username="testuser",
            ),
            study_name="test_config_20260101-120000",
            sweep=sweep,
        )

        assert "export MLFLOW_TRACKING_URI=http://localhost:5000" in script
        username_line = (
            "export MLFLOW_TRACKING_USERNAME="
            "${MLFLOW_TRACKING_USERNAME:-${JERNERICS_MLFLOW_USERNAME}}"
        )
        assert username_line in script
        assert "export MLFLOW_TRACKING_PASSWORD=${JERNERICS_MLFLOW_PASSWORD}" in script
        assert "export JERNERICS_MLFLOW_REMOTE_URI=http://localhost:5000" in script

    def test_mlflow_env_vars_absent_when_no_config(self, tmp_path):
        sweep = SweepConfig(
            _base={},
            search_space=None,
            n_trials=5,
            sampler=None,
            objective_task=None,
            objective_metric=None,
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        script = _generate_sweep_script(
            array_spec="1-5",
            slurm_opts={"partition": "gpu"},
            dag_relpath="dag.py",
            config_relpath="config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="test-project",
            mlflow_config=MlflowConfig(),
            study_name="test_config_20260101-120000",
            sweep=sweep,
        )

        assert "export MLFLOW_TRACKING_URI" not in script
        assert "export MLFLOW_TRACKING_USERNAME" not in script

    def test_objective_extraction(self, tmp_path):
        sweep = SweepConfig(
            _base={"seed": 42},
            search_space=None,
            n_trials=10,
            sampler=None,
            objective_task="train",
            objective_metric="loss",
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        script = _generate_sweep_script(
            array_spec="1-10",
            slurm_opts={"partition": "gpu"},
            dag_relpath="dag.py",
            config_relpath="config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="test-project",
            mlflow_config=MlflowConfig(),
            study_name="test_config_20260101-120000",
            sweep=sweep,
        )

        assert "results[sweep.objective_task]" in script
        assert "sweep.objective_metric" in script
        assert "study.tell(trial, value)" in script

    def test_optuna_fail_state(self, tmp_path):
        sweep = SweepConfig(
            _base={},
            search_space=None,
            n_trials=10,
            sampler=None,
            objective_task=None,
            objective_metric=None,
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        script = _generate_sweep_script(
            array_spec="1-10",
            slurm_opts={"partition": "gpu"},
            dag_relpath="dag.py",
            config_relpath="config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="test-project",
            mlflow_config=MlflowConfig(),
            study_name="test_config_20260101-120000",
            sweep=sweep,
        )

        assert "optuna.trial.TrialState.FAIL" in script

    def test_project_name_passed_to_dag(self, tmp_path):
        sweep = SweepConfig(
            _base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            objective_task=None,
            objective_metric=None,
            direction="minimize",
            slurm={},
            max_workers=None,
            executor_type=None,
        )

        script = _generate_sweep_script(
            array_spec="1-1",
            slurm_opts={"partition": "gpu"},
            dag_relpath="dag.py",
            config_relpath="config.py",
            remote_dir="~/experiments/test",
            hpc_config=_make_hpc_config(),
            binds={},
            project_name="my-project",
            mlflow_config=MlflowConfig(),
            study_name="test_20260101",
            sweep=sweep,
        )

        assert "project_name='my-project'" in script
