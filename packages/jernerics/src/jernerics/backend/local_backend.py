import itertools

import optuna
from optuna.storages.journal import JournalFileBackend, JournalStorage

from jernerics.backend.models import JobInfo, SubmitResult, SweepSpec
from jernerics.config import load_config
from jernerics.paths import cache_dir
from jernerics.runner import run_trial


class UnsupportedOperation(Exception):
    pass


class LocalBackend:
    def __init__(self, tracking_server: str | None = None):
        self.tracking_server = tracking_server

    def storage_path(self, study_name: str, project_name: str) -> str:
        project_cache = cache_dir()
        return str(project_cache / "optuna" / f"{study_name}.journal")

    def submit_sweep(
        self, spec: SweepSpec, *, direction: str = "minimize"
    ) -> SubmitResult:
        project_cache = cache_dir()
        tracker_dir = spec.tracking_dir or (
            project_cache / "tracking" / spec.study_name
        )
        tracker_dir.mkdir(parents=True, exist_ok=True)

        storage = JournalStorage(JournalFileBackend(spec.storage_url))
        sweep = load_config(str(spec.config_path))
        study = optuna.create_study(
            study_name=spec.study_name,
            storage=storage,
            direction=direction,
            sampler=sweep.sampler,
            load_if_exists=True,
        )

        if spec.grid:
            keys = sorted(spec.grid.keys())
            for combo in itertools.product(*[spec.grid[k] for k in keys]):
                study.enqueue_trial(dict(zip(keys, combo, strict=True)))

        any_failed = False

        for i in range(spec.n_trials):
            print(f"Running trial {i + 1}/{spec.n_trials}", flush=True)

            try:
                run_trial(
                    dag_file=str(spec.dag_path),
                    config_file=str(spec.config_path),
                    study_name=spec.study_name,
                    storage_url=spec.storage_url,
                    tracking_dir=str(tracker_dir),
                    project_name=spec.project_name,
                    server_addr=spec.server_addr or self.tracking_server,
                )
            except SystemExit as e:
                if e.code != 0:
                    any_failed = True
            except Exception:
                any_failed = True

        if any_failed:
            raise RuntimeError("One or more trials failed")

        return SubmitResult(job_id="local")

    def list_jobs(self, include_completed: bool = False) -> list[JobInfo]:
        raise UnsupportedOperation("LocalBackend does not support job listing")

    def cancel(self, job_id: str) -> bool:
        raise UnsupportedOperation("LocalBackend does not support job cancellation")

    def cancel_all(self) -> bool:
        raise UnsupportedOperation("LocalBackend does not support job cancellation")

    def get_status(self, job_id: str) -> str | None:
        raise UnsupportedOperation("LocalBackend does not support job status queries")

    def wait_for_completion(
        self, job_id: str, poll_interval: float = 30, timeout: float | None = None
    ) -> bool:
        raise UnsupportedOperation(
            "LocalBackend.submit_sweep is blocking; wait_for_completion is unnecessary"
        )

    def prepare_and_submit(
        self,
        spec: SweepSpec,
        *,
        project_dir,
        project_name: str,
        direction: str,
        dry_run: bool = False,
        backend_name: str = "",
        experiment_overrides=None,
        cli_overrides=None,
        local_cache_dir=None,
    ):
        raise UnsupportedOperation(
            "LocalBackend uses submit_sweep directly via 'jernerics local'"
        )

    def build(
        self,
        project_dir,
        *,
        project_name: str,
        force: bool = False,
        dry_run: bool = False,
        local_cache_dir=None,
    ) -> None:
        raise UnsupportedOperation("LocalBackend does not support remote builds")

    def clean(
        self, project_name: str, *, full: bool = False, force: bool = False
    ) -> None:
        raise UnsupportedOperation("LocalBackend does not support remote cleaning")

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        local_cache_dir=None,
    ) -> None:
        raise UnsupportedOperation("LocalBackend does not support remote log viewing")

    def sync(
        self,
        project_name: str,
        *,
        study: str | None = None,
    ) -> None:
        raise UnsupportedOperation("LocalBackend does not support remote sync")
