import sys
from pathlib import Path
from typing import Any

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.config import load_config
from jernerics.dag import DAG
from jernerics.tracking.trial_environment import TrialEnvironment


class _TaskFailure(Exception):
    pass


def run_trial(
    dag_file: str,
    config_file: str,
    study_name: str,
    storage_url: str,
    tracking_dir: str | None = None,
    project_name: str | None = None,
    server_addr: str | None = None,
    heartbeat_interval_s: float = 60.0,
) -> None:
    dag_dir = Path(dag_file).parent
    if str(dag_dir) not in sys.path:
        sys.path.insert(0, str(dag_dir))

    sweep = load_config(config_file)
    dag = DAG(dag_file, project_name)
    study = optuna.load_study(
        study_name=study_name,
        storage=JournalStorage(JournalFileBackend(storage_url)),
    )

    def objective(trial: optuna.trial.Trial) -> float:
        with TrialEnvironment(
            tracking_dir=tracking_dir or "",
            project_name=project_name or "",
            study_name=study_name,
            trial_number=trial.number,
            server_addr=server_addr,
            heartbeat_interval_s=heartbeat_interval_s,
        ) as env:
            params: dict[str, Any] = (
                sweep.search_space(trial) if sweep.search_space else {}
            )

            if params and set(sweep.base) & set(params):
                overlap = sorted(set(sweep.base) & set(params))
                raise ValueError(
                    f"Config keys defined in both base and search_space: {overlap}. "
                    "Please remove the overlapping keys from either 'base' or "
                    "'search_space' in the config file."
                )

            config = {**sweep.base, **params, "config_index": trial.number}

            results = dag.run(
                config,
                tracker=env.tracker,
                runner=sweep.runner,
            )

            failed_tasks = [
                (name, res) for name, res in results.items() if res.is_error
            ]
            if failed_tasks:
                for task_name, task_result in failed_tasks:
                    print(
                        f"\t[{task_name}] Task failed with exception:",
                        file=sys.stderr,
                    )
                    print(task_result.error_traceback, file=sys.stderr)
                raise _TaskFailure

            print(f"Trial {trial.number + 1} completed", file=sys.stderr)

            if sweep.objective:
                return sweep.objective(results)
            return 0.0

    try:
        study.optimize(objective, n_trials=1)
    except _TaskFailure:
        sys.exit(1)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("dag_file")
    parser.add_argument("config_file")
    parser.add_argument("--study-name")
    parser.add_argument("--storage-url")
    parser.add_argument("--tracking-dir")
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--server-addr", default=None)
    parser.add_argument("--heartbeat-interval", type=float, default=60.0)
    args = parser.parse_args()

    run_trial(
        dag_file=args.dag_file,
        config_file=args.config_file,
        study_name=args.study_name,
        storage_url=args.storage_url,
        tracking_dir=args.tracking_dir,
        project_name=args.project_name,
        server_addr=args.server_addr,
        heartbeat_interval_s=args.heartbeat_interval,
    )
