from pathlib import Path
from typing import Any, Protocol

from .models import JobInfo, SubmitResult, SweepSpec


class Backend(Protocol):
    def storage_path(self, study_name: str, project_name: str) -> str: ...
    def submit_sweep(
        self, spec: SweepSpec, *, direction: str = "minimize"
    ) -> SubmitResult: ...

    def list_jobs(self, include_completed: bool = False) -> list[JobInfo]: ...

    def cancel(self, job_id: str) -> bool: ...

    def cancel_all(self) -> bool: ...

    def get_status(self, job_id: str) -> str | None: ...

    def wait_for_completion(
        self, job_id: str, poll_interval: float = 30, timeout: float | None = None
    ) -> bool: ...

    def prepare_and_submit(
        self,
        spec: SweepSpec,
        *,
        project_dir: Path,
        project_name: str,
        direction: str,
        dry_run: bool = False,
        backend_name: str = "",
        experiment_overrides: dict[str, Any] | None = None,
        cli_overrides: dict[str, str] | None = None,
        local_cache_dir: Path | None = None,
    ) -> SubmitResult | None: ...

    def build(
        self,
        project_dir: Path,
        *,
        project_name: str,
        force: bool = False,
        dry_run: bool = False,
        local_cache_dir: Path | None = None,
    ) -> None: ...

    def clean(
        self,
        project_name: str,
        *,
        full: bool = False,
        force: bool = False,
    ) -> None: ...

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        local_cache_dir: Path | None = None,
    ) -> None: ...

    def sync(
        self,
        project_name: str,
        *,
        study: str | None = None,
    ) -> None: ...
