import json
import time
from pathlib import Path
from typing import Any

from jernerics.backend.components.command_builders import (
    build_checker_command,
    build_setup_command,
    build_trial_command,
)
from jernerics.backend.components.job_meta import save_job_meta
from jernerics.backend.components.path_resolver import PathResolver
from jernerics.backend.models import JobInfo, SubmitResult, SweepSubmission
from jernerics.config import ApptainerConfig, BackendConfig, PueueConfig
from jernerics.retry import RetryContext


def _parse_pueue_status(status_json: dict) -> list[JobInfo]:
    tasks = status_json.get("tasks", {})
    jobs = []
    for task_id_str, task in tasks.items():
        status = _pueue_status_to_str(task["status"])
        jobs.append(
            JobInfo(
                job_id=task_id_str,
                name=task.get("label") or task.get("original_command", "")[:40],
                status=status,
            )
        )
    return jobs


def _pueue_status_to_str(status: dict) -> str:
    if "Queued" in status:
        return "QUEUED"
    if "Running" in status:
        return "RUNNING"
    if "Done" in status:
        result = status["Done"].get("result", "")
        if result == "Success":
            return "COMPLETED"
        return "FAILED"
    if "Stashed" in status:
        return "STASHED"
    if "Locked" in status:
        return "LOCKED"
    return "UNKNOWN"


def _task_is_done(status: dict) -> bool:
    return "Done" in status


def _task_succeeded(status: dict) -> bool:
    if "Done" not in status:
        return False
    return status["Done"].get("result") == "Success"


class PueueDaemonError(RuntimeError):
    """Raised when the pueue daemon is unreachable."""


def _query_pueue_status(host) -> dict:
    """Query pueue status. Raises PueueDaemonError if daemon is unreachable."""
    result = host.run(
        ["pueue", "status", "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PueueDaemonError(
            f"pueue daemon is not running or unreachable: {result.stderr.strip()}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise PueueDaemonError(
            f"pueue returned invalid JSON (daemon may be misconfigured): {e}"
        ) from e


class PueueBackend:
    """Pueue-based backend for task execution with Docker.

    Works for both local and remote execution via the Host protocol.
    Use LocalHost for local execution, SSHHost for remote.
    """

    def __init__(
        self,
        host,
        container,
        *,
        remote_dir: str,
        cache_dir: str,
        tracking_server: str | None = None,
        parallel: int = 1,
        syncer=None,
        heartbeat_interval_s: float = 60.0,
        auto_retry: bool = False,
        stale_after_s: int = 120,
        grace_period_s: int = 120,
        max_retries: int = 3,
        chain_depth_cap: int = 20,
        build_dir: str | None = None,
        project_name: str = "",
    ):
        self.host = host
        self.container = container
        self.remote_dir = remote_dir
        self.cache_dir = cache_dir
        self.tracking_server = tracking_server
        self.parallel = parallel
        self.syncer = syncer
        self.heartbeat_interval_s = heartbeat_interval_s
        self.auto_retry = auto_retry
        self.stale_after_s = stale_after_s
        self.grace_period_s = grace_period_s
        self.max_retries = max_retries
        self.chain_depth_cap = chain_depth_cap

        self._paths = PathResolver(
            remote_dir=remote_dir,
            cache_dir=cache_dir,
            container=container,
            build_dir=build_dir,
            project_name=project_name,
        )

    def generate_submit_job(
        self, script: str, *, name: str, log_dir: str | None = None
    ) -> str:
        # Script content must not contain single quotes.
        return (
            f"BUILD_ID=$(pueue add --label {name}"
            f" -- bash -e -c '{script}'"
            " 2>&1 | grep -oE '[0-9]+')\n"
            "echo $BUILD_ID"
        )

    @classmethod
    def from_config(
        cls,
        backend_config: BackendConfig,
        *,
        host,
        syncer=None,
        tracking_server: str | None = None,
        project_name: str = "",
    ) -> "PueueBackend":
        from jernerics.backend.components.container import (
            Apptainer,
            Docker,
            NoContainer,
        )

        assert isinstance(backend_config.backend, PueueConfig)
        pueue = backend_config.backend

        shared = backend_config.shared
        remote_dir = shared.remote_dir.replace("~", "$HOME")
        cache_dir = shared.cache_dir or "$HOME/.cache/jernerics"

        container_type = shared.container_type
        if container_type == "docker":
            container = Docker()
        elif container_type == "apptainer":
            container = Apptainer()
        elif container_type == "none":
            container = NoContainer()
        else:
            container = Docker()

        build_dir = None
        if isinstance(backend_config.container, ApptainerConfig):
            build_dir = backend_config.container.build_dir

        return cls(
            host=host,
            container=container,
            remote_dir=remote_dir,
            cache_dir=cache_dir,
            tracking_server=tracking_server,
            parallel=pueue.parallel,
            syncer=syncer,
            heartbeat_interval_s=shared.heartbeat_interval_s,
            auto_retry=shared.auto_retry,
            stale_after_s=shared.stale_after_s,
            grace_period_s=shared.grace_period_s,
            max_retries=shared.max_retries,
            chain_depth_cap=shared.chain_depth_cap,
            build_dir=build_dir,
            project_name=project_name,
        )

    def storage_path(self, study_name: str) -> str:
        return self._paths.storage_path(study_name)

    def _generate_submit_script(
        self,
        spec: SweepSubmission,
        *,
        direction: str = "minimize",
        retry_ctx: RetryContext | None = None,
    ) -> str:
        cache_host = self._paths.resolve_cache()
        bind_args = self._paths.bind_args(cache_host)
        tracking_dir = self._paths.tracking_dir(spec.study_name)

        dag_relpath = spec.dag_relpath or str(spec.dag_path.name)
        config_relpath = spec.config_relpath or str(spec.config_path.name)

        setup_cmd = build_setup_command(
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            direction=direction,
            config_relpath=config_relpath,
            grid=spec.grid,
            work_prefix=self._paths.work_prefix,
            cache_prefix=self._paths.cache_prefix,
        )
        wrapped_setup = self.container.wrap(setup_cmd, bind_args)

        trial_cmd = build_trial_command(
            dag_relpath=dag_relpath,
            config_relpath=config_relpath,
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            project_name=spec.project_name,
            tracking_dir=tracking_dir,
            tracking_server=self.tracking_server,
            work_prefix=self._paths.work_prefix,
        )
        wrapped_trial = self.container.wrap(trial_cmd, bind_args)

        group = spec.study_name
        max_parallel = spec.max_parallel or self.parallel

        setup_path = f"/tmp/jernerics_{spec.study_name}_setup.sh"
        trial_path = f"/tmp/jernerics_{spec.study_name}_trial.sh"

        lines = [
            f"pueue group add {group} 2>/dev/null || true",
            f"pueue parallel {max_parallel} --group {group}",
            f"mkdir -p {cache_host}/optuna {cache_host}/tracking/{spec.study_name}",
            "",
            f"cat > {setup_path} << 'JERNERICS_EOF'",
            wrapped_setup,
            "JERNERICS_EOF",
            "",
            f"cat > {trial_path} << 'JERNERICS_EOF'",
            wrapped_trial,
            "JERNERICS_EOF",
            "",
            f"SETUP_ID=$(pueue add -g {group}"
            f" --label {spec.study_name}_setup"
            f" -- bash {setup_path} 2>&1 | grep -oE '[0-9]+')",
        ]

        for i in range(spec.n_trials):
            lines.append(
                f"TRIAL_{i + 1}_ID=$(pueue add -g {group} --after $SETUP_ID"
                f" --label {spec.study_name}_trial_{i + 1}"
                f" -- bash {trial_path} 2>&1 | grep -oE '[0-9]+')"
            )

        if retry_ctx is not None:
            checker_cmd = build_checker_command(
                retry_ctx.ctx_path, retry_ctx.chain_depth
            )
            retry_script = (
                f"/tmp/jernerics_{spec.study_name}_retry_d{retry_ctx.chain_depth}.sh"
            )
            wrapped_checker = self.container.wrap(
                f"{checker_cmd} 2>/dev/null > {retry_script} && bash {retry_script}",
                bind_args,
            )

            trial_ids = " ".join(f"$TRIAL_{i + 1}_ID" for i in range(spec.n_trials))
            checker_inner_path = f"/tmp/jernerics_{spec.study_name}_checker.sh"
            checker_wrapper_path = f"/tmp/jernerics_{spec.study_name}_wait_and_check.sh"
            lines.append("")
            lines.append(f"cat > {checker_inner_path} << 'JERNERICS_EOF'")
            lines.append(wrapped_checker)
            lines.append("JERNERICS_EOF")
            lines.append("")
            lines.append(
                f"cat > {checker_wrapper_path} <<JERNERICS_EOF\n"
                f"pueue wait {trial_ids} -q\n"
                f"bash {checker_inner_path}\n"
                "JERNERICS_EOF"
            )
            lines.append("")

            lines.append(
                f"pueue add -g {group}"
                f" --label {spec.study_name}_checker"
                f" -- bash {checker_wrapper_path}"
            )

        return "\n".join(lines)

    def submit_sweep(
        self,
        spec: SweepSubmission,
        *,
        direction: str = "minimize",
        retry_ctx: RetryContext | None = None,
    ) -> SubmitResult:
        script = self._generate_submit_script(
            spec, direction=direction, retry_ctx=retry_ctx
        )
        group = spec.study_name

        result = self.host.run(
            ["bash"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit sweep: {result.stderr.strip()}")

        return SubmitResult(job_id=group)

    def list_jobs(self, include_completed: bool = False) -> list[JobInfo]:
        data = _query_pueue_status(self.host)
        jobs = _parse_pueue_status(data)
        if not include_completed:
            jobs = [j for j in jobs if j.status not in ("COMPLETED", "FAILED")]
        return jobs

    def cancel(self, job_id: str) -> bool:
        if job_id.isdigit():
            result = self.host.run(
                ["pueue", "kill", job_id],
                check=False,
                capture_output=True,
            )
            return result.returncode == 0
        result = self.host.run(
            ["pueue", "kill", "--group", job_id],
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    def cancel_all(self) -> bool:
        self.host.run(
            ["pueue", "kill", "--all"],
            check=False,
            capture_output=True,
        )
        return True

    def get_status(self, job_id: str) -> str | None:
        data = _query_pueue_status(self.host)
        tasks = data.get("tasks", {})
        task = tasks.get(job_id)
        if task is None:
            return None
        return _pueue_status_to_str(task["status"])

    def wait_for_completion(
        self, job_id: str, poll_interval: float = 30, timeout: float | None = None
    ) -> bool:
        start_time = time.time()
        while True:
            if timeout is not None and (time.time() - start_time) >= timeout:
                raise TimeoutError(
                    f"Timeout waiting for job {job_id} after {timeout} seconds"
                )

            data = _query_pueue_status(self.host)
            tasks = data.get("tasks", {})

            if job_id.isdigit():
                task = tasks.get(job_id)
                if task is None:
                    return True
                if _task_is_done(task["status"]):
                    return _task_succeeded(task["status"])
            else:
                group_tasks = [t for t in tasks.values() if t.get("group") == job_id]
                if not group_tasks:
                    return True
                if all(_task_is_done(t["status"]) for t in group_tasks):
                    return all(_task_succeeded(t["status"]) for t in group_tasks)

            time.sleep(poll_interval)

    def get_log(self, task_id: str) -> str:
        result = self.host.run(
            ["pueue", "log", task_id, "--json"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return result.stderr
        try:
            data = json.loads(result.stdout)
            task_data = data.get(task_id, {})
            return task_data.get("output", "")
        except json.JSONDecodeError:
            return result.stdout

    def prepare_and_submit(
        self,
        spec: SweepSubmission,
        *,
        project_dir: Path,
        project_name: str,
        direction: str,
        dry_run: bool = False,
        backend_name: str = "",
        experiment_overrides: dict[str, Any] | None = None,
        cli_overrides: dict[str, str] | None = None,
        local_cache_dir: Path | None = None,
    ) -> SubmitResult | None:
        if dry_run:
            print("=== DRY RUN ===")
            print(f"Backend: {backend_name} (pueue)")
            print(f"Group: {spec.study_name}")
            print(f"Trials: {spec.n_trials}")
            return None

        from jernerics.backend.orchestration import prepare_and_submit

        def save_meta(result: SubmitResult) -> None:
            if local_cache_dir is not None:
                save_job_meta(
                    job_id=result.job_id,
                    backend="pueue",
                    remote_dir=self.remote_dir,
                    n_trials=spec.n_trials,
                    local_cache_dir=local_cache_dir,
                )

        result = prepare_and_submit(
            host=self.host,
            container=self.container,
            syncer=self.syncer,
            paths=self._paths,
            remote_dir=self.remote_dir,
            spec=spec,
            project_dir=project_dir,
            project_name=project_name,
            direction=direction,
            backend_name=backend_name,
            auto_retry=self.auto_retry,
            local_cache_dir=local_cache_dir,
            cli_overrides=cli_overrides,
            ensure_submission_ready=lambda: None,
            submit_sweep=self.submit_sweep,
            save_meta=save_meta,
        )

        retry_suffix = (
            " (with auto-retry)"
            if self.auto_retry and local_cache_dir is not None
            else ""
        )
        print(f"\nSweep submitted: group {result.job_id}{retry_suffix}")
        return result

    def build(
        self,
        project_dir: Path,
        *,
        project_name: str,
        force: bool = False,
        dry_run: bool = False,
        local_cache_dir: Path | None = None,
    ) -> None:
        from jernerics.backend.orchestration import submit_build

        submit_build(
            host=self.host,
            container=self.container,
            syncer=self.syncer,
            paths=self._paths,
            remote_dir=self.remote_dir,
            project_dir=project_dir,
            project_name=project_name,
            force=force,
            dry_run=dry_run,
            generate_submit_job=self.generate_submit_job,
        )

    def clean(
        self,
        project_name: str,
        *,
        full: bool = False,
        force: bool = False,
    ) -> None:
        from jernerics.backend.orchestration import clean as shared_clean

        def list_active():
            try:
                data = _query_pueue_status(self.host)
                return [
                    j
                    for j in _parse_pueue_status(data)
                    if j.status not in ("COMPLETED", "FAILED", "STASHED", "LOCKED")
                ]
            except PueueDaemonError:
                return []

        def scheduler_cleanup():
            self.host.run(["pueue", "clean"], check=False, capture_output=True)

        shared_clean(
            host=self.host,
            paths=self._paths,
            remote_dir=self.remote_dir,
            project_name=project_name,
            full=full,
            force=force,
            list_active_jobs=list_active,
            scheduler_cleanup=scheduler_cleanup,
        )

    def sync(
        self,
        project_name: str,
        *,
        study: str | None = None,
    ) -> None:
        from jernerics.backend.orchestration import sync as shared_sync

        shared_sync(
            host=self.host,
            container=self.container,
            paths=self._paths,
            remote_dir=self.remote_dir,
            project_name=project_name,
            tracking_server=self.tracking_server,
            study=study,
        )

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        local_cache_dir: Path | None = None,
    ) -> None:
        if not job_id.isdigit():
            print("Error: For pueue backends, specify a task ID (integer).")
            raise SystemExit(1)

        if follow:
            self.host.run(
                ["pueue", "follow", job_id],
                check=False,
            )
        else:
            output = self.get_log(job_id)
            print(output)
