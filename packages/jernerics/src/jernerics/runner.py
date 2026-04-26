import sys
from pathlib import Path
from typing import Any

import grpc
import optuna
from jernerics_proto import tracking_pb2_grpc

from jernerics._cli_helpers import load_config
from jernerics.dag import DAG
from jernerics.tracking import ProtobufTracker, Tracker
from jernerics.tracking.client import FileSyncClient


def run_trial(
    dag_file: str,
    config_file: str,
    study_name: str,
    storage_url: str,
    tracking_dir: str | None = None,
    project_name: str | None = None,
    server_addr: str | None = None,
) -> None:
    dag_dir = Path(dag_file).parent
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

    tracker: Tracker | None = None
    sync_client: FileSyncClient | None = None
    channel: grpc.Channel | None = None

    if tracking_dir:
        tracker = ProtobufTracker(
            project_name or "",
            study_name,
            trial.number,
            Path(tracking_dir) / f"{trial.number}.pb",
        )

    if server_addr and tracker:
        assert tracking_dir is not None
        channel = grpc.insecure_channel(server_addr)
        stub = tracking_pb2_grpc.TrackingServiceStub(channel)
        sync_client = FileSyncClient(stub, Path(tracking_dir) / f"{trial.number}.pb")
        sync_client.start()
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
            tracker=tracker,
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
    finally:
        if tracker:
            tracker.close()
        if sync_client:
            sync_client.join()
        if channel:
            channel.close()


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
    args = parser.parse_args()

    run_trial(
        dag_file=args.dag_file,
        config_file=args.config_file,
        study_name=args.study_name,
        storage_url=args.storage_url,
        tracking_dir=args.tracking_dir,
        project_name=args.project_name,
        server_addr=args.server_addr,
    )
