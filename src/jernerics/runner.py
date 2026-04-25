import pathlib
import sys
from typing import Any

import optuna

from jernerics._cli_helpers import load_config
from jernerics.dag import DAG


def run_trial(
    dag_file: str,
    config_file: str,
    study_name: str,
    storage_url: str,
    project_name: str | None = None,
) -> None:
    dag_dir = pathlib.Path(dag_file).parent
    if str(dag_dir) not in sys.path:
        sys.path.insert(0, str(dag_dir))

    sweep = load_config(config_file)
    dag = DAG(dag_file, project_name)

    study = optuna.load_study(study_name=study_name, storage=storage_url)

    # NOTE: the code uses a _tell() helper instead of bare study.tell()
    # because GridSampler.after_trial() calls study.stop() when all grid points
    # are exhausted, which raises RuntimeError in ask/tell mode (it is designed
    # for study.optimize()). The trial is already recorded at that point, so we
    # safely swallow the error. See: https://github.com/optuna/optuna/issues/5106
    def _tell(study, trial, value=None, *, state=None):
        try:
            if state is not None:
                study.tell(trial, state=state)
            else:
                study.tell(trial, value)
        except RuntimeError:
            pass

    trial = study.ask()
    # Allow search_space to be None for the edge case where the
    # user wants no hyperparameters.
    params: dict[str, Any] = sweep.search_space(trial) if sweep.search_space else {}

    if params and set(sweep.base) & set(params):
        overlap = sorted(set(sweep.base) & set(params))
        raise ValueError(
            f"Config keys defined in both base and search_space: {overlap}. "
            "Please remove the overlapping keys from either 'base' or "
            "'search_space' in the config file."
        )

    config = {**sweep.base, **params}

    try:
        results = dag.run(
            config,
            config_index=trial.number,
            config_path=config_file,
            runner=sweep.runner,
        )

        failed_tasks = [(name, res) for name, res in results.items() if res.is_error]
        if failed_tasks:
            for task_name, task_result in failed_tasks:
                print(f"\t[{task_name}] Task failed with exception:", file=sys.stderr)
                print(task_result.error_traceback, file=sys.stderr)
            _tell(study, trial, state=optuna.trial.TrialState.FAIL)
            sys.exit(1)

        if sweep.objective:
            value = sweep.objective(results)
            _tell(study, trial, value)
        else:
            _tell(study, trial, 0.0)

        print(f"Trial {trial.number + 1} completed", file=sys.stderr)
    except Exception:
        _tell(study, trial, state=optuna.trial.TrialState.FAIL)
        raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("dag_file")
    parser.add_argument("config_file")
    parser.add_argument("--study-name")
    parser.add_argument("--storage-url")
    parser.add_argument("--project-name", default=None)
    args = parser.parse_args()

    run_trial(
        dag_file=args.dag_file,
        config_file=args.config_file,
        study_name=args.study_name,
        storage_url=args.storage_url,
        project_name=args.project_name,
    )
