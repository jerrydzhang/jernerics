from jernerics.config import HpcConfig, SweepConfig


def _generate_sweep_script(
    array_spec: str,
    slurm_opts: dict[str, str],
    dag_relpath: str,
    config_relpath: str,
    remote_dir: str,
    hpc_config: HpcConfig,
    binds: dict[str, str],
    project_name: str,
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
        "tracking_server": None,
    }
    defaults.update(overrides)
    return HpcConfig(**defaults)


class TestSweepScriptGeneration:
    def test_sweep_script_contains_optuna(self, tmp_path):
        sweep = SweepConfig(
            base={"seed": 42},
            search_space=lambda trial: {"lr": trial.suggest_float("lr", 1e-5, 1e-1)},
            n_trials=50,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
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
            study_name="test-project_config_20260101-120000",
            sweep=sweep,
        )

        assert "import optuna" in script
        assert "optuna.create_study" in script
        assert "jernerics.runner" in script
        assert "#SBATCH --array=1-50%10" in script

    def test_sweep_script_single_trial(self, tmp_path):
        sweep = SweepConfig(
            base={"seed": 42},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
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
            study_name="test-project_config_20260101-120000",
            sweep=sweep,
        )

        assert "#SBATCH --array=1-1" in script
        assert "jernerics.runner" in script

    def test_array_spec_calculation(self):
        from jernerics.cli import DEFAULT_SLURM

        assert DEFAULT_SLURM == {}

    def test_study_name_format(self, tmp_path):
        sweep = SweepConfig(
            base={},
            search_space=None,
            n_trials=10,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
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
            study_name=study_name,
            sweep=sweep,
        )

        assert f"study_name={study_name!r}" in script
        assert "sqlite:///" in script
        assert "/cache/optuna/" in script

    def test_no_mlflow_env_vars(self, tmp_path):
        sweep = SweepConfig(
            base={},
            search_space=None,
            n_trials=5,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
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
            study_name="test_config_20260101-120000",
            sweep=sweep,
        )

        assert "MLFLOW" not in script

    def test_objective_extraction(self, tmp_path):
        sweep = SweepConfig(
            base={"seed": 42},
            search_space=None,
            n_trials=10,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
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
            study_name="test_config_20260101-120000",
            sweep=sweep,
        )

        assert "jernerics.runner" in script
        assert "--study-name" in script
        assert "test_config_20260101-120000" in script
        assert "jernerics.runner" in script

    def test_optuna_fail_state(self, tmp_path):
        sweep = SweepConfig(
            base={},
            search_space=None,
            n_trials=10,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
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
            study_name="test_config_20260101-120000",
            sweep=sweep,
        )

        assert "jernerics.runner" in script

    def test_project_name_passed_to_dag(self, tmp_path):
        sweep = SweepConfig(
            base={},
            search_space=None,
            n_trials=1,
            sampler=None,
            direction="minimize",
            slurm={},
            runner=None,
            objective=None,
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
            study_name="test_20260101",
            sweep=sweep,
        )

        assert "--project-name" in script
        assert "my-project" in script
