import base64
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import optuna
from jernerics_schema import (
    ExecutionId,
    ExecutionOutcome,
    FailureKind,
    SweepId,
    TrialId,
    TrialState,
    ValueEvent,
    sweep_id_for,
)
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.config import load_config
from jernerics.optuna_mirror import TRIAL_ID_ATTR, snapshot_kwargs
from jernerics.tracking import Tracker
from jernerics.tracking.jsonl_io import TrackingReader
from jernerics.tracking.trial_environment import TrialEnvironment
from jernerics.trial_context import (
    EXECUTION_ID_ENV,
    PROJECT_NAME_ENV,
    STUDY_NAME_ENV,
    SWEEP_ID_ENV,
    TRACKING_DIR_ENV,
    TRIAL_CONFIG_ENV,
    TRIAL_ID_ENV,
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
    param_overrides: dict[str, Any] | None = None,
) -> None:
    """Run one trial through the explicit ask/tell optimizer lifecycle.

    ``study.ask`` allocates the optimizer trial; search-space evaluation,
    child execution, and objective evaluation run under Jernerics control;
    ``study.tell`` commits the terminal optimizer state (objective on
    success, FAIL otherwise) before the execution ends. Trial snapshots
    mirror the optimizer state after ask and after tell.
    """
    sweep = load_config(config_file)

    study = optuna.load_study(
        study_name=study_name,
        storage=JournalStorage(JournalFileBackend(storage_url)),
    )
    sweep_id = sweep_id_for(project_name or "", study_name)
    run: dict[str, Any] = {"environment": None, "exit_code": None}

    trial = study.ask()
    try:
        environment = TrialEnvironment(
            tracking_dir=tracking_dir or "",
            trial_number=trial.number,
            server_addr=server_addr,
            heartbeat_interval_s=heartbeat_interval_s,
        )
        run["environment"] = environment
        environment.start()
        if environment.trial_id is not None:
            trial.set_user_attr(TRIAL_ID_ATTR, str(environment.trial_id))

        params: dict[str, Any] = {}
        if sweep.grid is not None:
            params = {
                key: trial.suggest_categorical(key, list(dict.fromkeys(values)))
                for key, values in sorted(sweep.grid.items())
            }
        elif sweep.search_space is not None:
            params = sweep.search_space(trial)

        if params and set(sweep.base) & set(params):
            overlap = sorted(set(sweep.base) & set(params))
            raise ValueError(
                f"Config keys defined in both base and search_space: {overlap}. "
                "Please remove the overlapping keys from either 'base' or "
                "'search_space' in the config file."
            )

        overrides = param_overrides or {}
        if overrides and set(overrides) & set(params):
            overlap = sorted(set(overrides) & set(params))
            raise ValueError(
                f"Config keys defined in both --set-param and search_space: "
                f"{overlap}. Please remove the overlapping keys from either "
                "--set-param or 'search_space' in the config file."
            )

        config = {
            **sweep.base,
            **params,
            **overrides,
            "config_index": trial.number,
        }

        config_path = _write_trial_config(config, tracking_dir, trial.number)
        events_path = Path(tracking_dir or "") / "events" / f"{trial.number}.jsonl"

        if environment.tracker is not None:
            environment.tracker.log_json("resolved_config", config)
        _emit_trial_snapshot(
            environment.tracker,
            trial,
            sweep_id=sweep_id,
            trial_id=environment.trial_id,
            state=TrialState.RUNNING,
        )

        logs_dir = Path(tracking_dir) / "logs" if tracking_dir else None
        stdout_path = stderr_path = None
        if logs_dir is not None:
            logs_dir.mkdir(parents=True, exist_ok=True)
            stdout_path = logs_dir / f"trial-{trial.number}.stdout"
            stderr_path = logs_dir / f"trial-{trial.number}.stderr"

        with ExitStack() as stack:
            completed = subprocess.run(
                [sys.executable, str(Path(trial_file).resolve())],
                env=_trial_env(
                    config_path=config_path,
                    tracking_dir=tracking_dir or "",
                    project_name=project_name or "",
                    study_name=study_name,
                    trial_number=trial.number,
                    sweep_id=sweep_id,
                    trial_id=environment.trial_id,
                    execution_id=environment.execution_id,
                ),
                check=False,
                stdout=(
                    stack.enter_context(open(stdout_path, "wb"))
                    if stdout_path
                    else None
                ),
                stderr=(
                    stack.enter_context(open(stderr_path, "wb"))
                    if stderr_path
                    else None
                ),
            )
        run["exit_code"] = completed.returncode
        _declare_system_logs(
            environment.tracker, trial.number, stdout_path, stderr_path
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
        value = sweep.objective(results) if sweep.objective else 0.0
    except BaseException as exc:
        try:
            study.tell(trial, state=optuna.trial.TrialState.FAIL)
            _emit_trial_snapshot(
                (run["environment"].tracker if run["environment"] else None),
                trial,
                sweep_id=sweep_id,
                trial_id=run["environment"].trial_id if run["environment"] else None,
                state=TrialState.FAILED,
            )
        finally:
            _finish_failure(run["environment"], exc, run)
        if isinstance(exc, _TaskFailure):
            sys.exit(1)
        raise

    try:
        study.tell(trial, value)
        _emit_trial_snapshot(
            run["environment"].tracker if run["environment"] else None,
            trial,
            sweep_id=sweep_id,
            trial_id=run["environment"].trial_id if run["environment"] else None,
            state=TrialState.COMPLETED,
            objective=value,
        )
    except BaseException as exc:
        _finish_failure(run["environment"], exc, run)
        raise
    if run["environment"] is not None:
        run["environment"].finish_execution(ExecutionOutcome.SUCCESS)


def _declare_system_logs(
    tracker: Tracker | None,
    trial_number: int,
    stdout_path: Path | None,
    stderr_path: Path | None,
) -> None:
    """Declare captured child stdout/stderr as system artifacts.

    The files flow through the normal manifest/upload path keyed
    ``stdout``/``stderr`` with source ``system``, bound to the current
    execution.
    """
    if tracker is None:
        return
    for key, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        if path is not None and path.exists():
            tracker.log_artifact(
                key,
                str(path),
                source="system",
                content_type="text/plain",
            )


def _emit_trial_snapshot(
    tracker: Tracker | None,
    trial: Any,
    *,
    sweep_id: SweepId,
    trial_id: TrialId | None,
    state: TrialState,
    objective: float | None = None,
) -> None:
    if tracker is None or trial_id is None:
        return
    tracker.emit_trial_snapshot(
        sweep_id=sweep_id,
        number=trial.number,
        state=state,
        objective=objective,
        **snapshot_kwargs(trial, trial_id=trial_id),
    )


def _finish_failure(
    environment: TrialEnvironment | None, exc: BaseException, run: dict[str, Any]
) -> None:
    if environment is None:
        return
    exit_code = run["exit_code"]
    if isinstance(exc, _TaskFailure):
        if exit_code is not None and exit_code < 0:
            environment.finish_execution(
                ExecutionOutcome.CANCELLED,
                exit_code=exit_code,
                failure_summary=f"child terminated by signal {-exit_code}",
            )
        else:
            environment.finish_execution(
                ExecutionOutcome.FAILURE,
                exit_code=exit_code,
                failure_kind=FailureKind.UNKNOWN,
                failure_summary=f"child process exited with code {exit_code}",
            )
        return
    environment.finish_execution(
        ExecutionOutcome.FAILURE,
        exit_code=exit_code if exit_code == 0 else None,
        failure_kind=FailureKind.EXCEPTION,
        failure_summary=repr(exc),
    )


def _write_trial_config(
    config: dict[str, Any], tracking_dir: str | None, config_index: int
) -> Path:
    cache_root = Path(tracking_dir).parent.parent if tracking_dir else Path.cwd()
    configs_dir = cache_root / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    path = configs_dir / f"trial_{config_index}.json"
    path.write_text(json.dumps(config, indent=2))
    return path


def _trial_env(
    *,
    config_path: Path,
    tracking_dir: str,
    project_name: str,
    study_name: str,
    trial_number: int,
    sweep_id: SweepId,
    trial_id: TrialId | None,
    execution_id: ExecutionId | None,
) -> dict[str, str]:
    env = {
        **os.environ,
        TRIAL_CONFIG_ENV: str(config_path),
        TRACKING_DIR_ENV: tracking_dir,
        PROJECT_NAME_ENV: project_name,
        STUDY_NAME_ENV: study_name,
        TRIAL_NUMBER_ENV: str(trial_number),
        SWEEP_ID_ENV: str(sweep_id),
    }
    if trial_id is not None:
        env[TRIAL_ID_ENV] = str(trial_id)
    if execution_id is not None:
        env[EXECUTION_ID_ENV] = str(execution_id)
    return env


def _read_trial_results(events_path: Path) -> dict[str, Any]:
    if not events_path.exists():
        return {}
    with TrackingReader(events_path) as reader:
        for event in reader:
            if isinstance(event, ValueEvent) and event.key == "results":
                if event.observation is not None:
                    return event.observation
                if isinstance(event.value, str):
                    return json.loads(event.value)
                return {}
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
    parser.add_argument("--param-overrides", default=None)
    args = parser.parse_args()

    cli_param_overrides = None
    if args.param_overrides:
        cli_param_overrides = json.loads(
            base64.b64decode(args.param_overrides).decode()
        )
    run_trial(
        trial_file=args.trial_file,
        config_file=args.config_file,
        study_name=args.study_name,
        storage_url=args.storage_url,
        tracking_dir=args.tracking_dir,
        project_name=args.project_name,
        server_addr=args.server_addr,
        heartbeat_interval_s=args.heartbeat_interval,
        param_overrides=cli_param_overrides,
    )
