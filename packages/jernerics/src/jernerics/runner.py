import runpy
import sys
from pathlib import Path
from typing import Any

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.config import load_config
from jernerics.tracking.trial_environment import TrialEnvironment


class _TaskFailure(Exception):
    pass


def run_trial(
    trial_file: str,
    config_file: str,
    study_name: str,
    storage_url: str,
    tracking_dir: str | None = None,
    project_name: str | None = None,
    server_addr: str | None = None,
    heartbeat_interval_s: float = 60.0,
    git_hash: str | None = None,
) -> None:
    trial_dir = Path(trial_file).parent
    if str(trial_dir) not in sys.path:
        sys.path.insert(0, str(trial_dir))

    sweep = load_config(config_file)
    sweep_config_text = Path(config_file).read_text()
    module = runpy.run_path(trial_file)
    if "trial" not in module or not callable(module["trial"]):
        raise RuntimeError(
            f"Trial file '{trial_file}' must define a callable 'trial(config, tracker)'"
        )
    trial_fn = module["trial"]

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
            git_hash=git_hash,
            sweep_config=sweep_config_text,
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

            try:
                results = trial_fn(config, env.tracker)
            except Exception as exc:
                print(f"Trial {trial.number + 1} failed: {exc}", file=sys.stderr)
                raise _TaskFailure from exc

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
    parser.add_argument("trial_file")
    parser.add_argument("config_file")
    parser.add_argument("--study-name")
    parser.add_argument("--storage-url")
    parser.add_argument("--tracking-dir")
    parser.add_argument("--project-name", default=None)
    parser.add_argument("--server-addr", default=None)
    parser.add_argument("--heartbeat-interval", type=float, default=60.0)
    parser.add_argument("--git-hash", default=None)
    args = parser.parse_args()

    run_trial(
        trial_file=args.trial_file,
        config_file=args.config_file,
        study_name=args.study_name,
        storage_url=args.storage_url,
        tracking_dir=args.tracking_dir,
        project_name=args.project_name,
        server_addr=args.server_addr,
        heartbeat_interval_s=args.heartbeat_interval,
        git_hash=args.git_hash,
    )
