import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.config import load_config
from jernerics.tracking.jsonl_io import TrackingReader
from jernerics.tracking.trial_environment import TrialEnvironment
from jernerics.trial_context import (
    PROJECT_NAME_ENV,
    RUN_ID_ENV,
    STUDY_NAME_ENV,
    TRACKING_DIR_ENV,
    TRIAL_CONFIG_ENV,
    TRIAL_NUMBER_ENV,
)


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
    sweep = load_config(config_file)
    sweep_config_text = Path(config_file).read_text()

    study = optuna.load_study(
        study_name=study_name,
        storage=JournalStorage(JournalFileBackend(storage_url)),
    )
    run_id = int(time.time())

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
            run_id=run_id,
        ):
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

            config_path = _write_trial_config(config, tracking_dir, trial.number)
            events_path = Path(tracking_dir or "") / "events" / f"{trial.number}.jsonl"

            completed = subprocess.run(
                [sys.executable, str(Path(trial_file).resolve())],
                env=_trial_env(
                    config_path=config_path,
                    tracking_dir=tracking_dir or "",
                    project_name=project_name or "",
                    study_name=study_name,
                    trial_number=trial.number,
                    run_id=run_id,
                ),
                check=False,
            )
            if completed.returncode != 0:
                print(
                    f"Trial {trial.number + 1} failed with exit code "
                    f"{completed.returncode}",
                    file=sys.stderr,
                )
                raise _TaskFailure

            print(f"Trial {trial.number + 1} completed", file=sys.stderr)

            results = _read_trial_results(events_path)

        if sweep.objective:
            return sweep.objective(results)
        return 0.0

    try:
        study.optimize(objective, n_trials=1)
    except _TaskFailure:
        sys.exit(1)


def _write_trial_config(
    config: dict[str, Any], tracking_dir: str | None, config_index: int
) -> Path:
    cache_root = Path(tracking_dir).parent.parent if tracking_dir else Path.cwd()
    configs_dir = cache_root / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    path = configs_dir / f"trial_{config_index}.json"
    path.write_text(json.dumps(config))
    return path


def _trial_env(
    *,
    config_path: Path,
    tracking_dir: str,
    project_name: str,
    study_name: str,
    trial_number: int,
    run_id: int,
) -> dict[str, str]:
    return {
        **os.environ,
        TRIAL_CONFIG_ENV: str(config_path),
        TRACKING_DIR_ENV: tracking_dir,
        PROJECT_NAME_ENV: project_name,
        STUDY_NAME_ENV: study_name,
        TRIAL_NUMBER_ENV: str(trial_number),
        RUN_ID_ENV: str(run_id),
    }


def _read_trial_results(events_path: Path) -> dict[str, Any]:
    if not events_path.exists():
        return {}
    with TrackingReader(events_path) as reader:
        for envelope in reader:
            value = envelope.get("value")
            if isinstance(value, dict) and value.get("key") == "results":
                return json.loads(value["value_json"])
    return {}


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
