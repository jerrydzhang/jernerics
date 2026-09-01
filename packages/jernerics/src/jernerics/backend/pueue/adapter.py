import json
import time

from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.models import JobInfo, JobSubmission, SubmitResult
from jernerics.backend.path_resolver import substitute_project_name
from jernerics.config import BackendConfig, PueueConfig


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


class PueueAdapter:
    def __init__(
        self,
        host,
        *,
        remote_dir: str,
        cache_dir: str,
        parallel: int = 1,
    ):
        self.host = host
        self.remote_dir = remote_dir
        self.cache_dir = cache_dir
        self.parallel = parallel

    @classmethod
    def from_config(
        cls,
        backend_config: BackendConfig,
        *,
        host,
        project_name: str = "",
    ) -> "PueueAdapter":
        assert isinstance(backend_config.backend, PueueConfig)
        pueue = backend_config.backend

        shared = backend_config.shared
        remote_dir = substitute_project_name(
            shared.remote_dir.replace("~", host.home), project_name
        )
        cache_dir = (
            shared.cache_dir.replace("~", host.home)
            if shared.cache_dir
            else f"{host.home}/.cache/jernerics"
        )

        return cls(
            host=host,
            remote_dir=remote_dir,
            cache_dir=cache_dir,
            parallel=pueue.parallel,
        )

    def _render_script(self, params: SweepSubmissionParams) -> str:
        group = params.study_name
        max_parallel = params.max_parallel or self.parallel

        setup_path = f"/tmp/jernerics_{group}_setup.sh"
        trial_path = f"/tmp/jernerics_{group}_trial.sh"

        lines = [
            f"pueue group add {group} 2>/dev/null || true",
            f"pueue parallel {max_parallel} --group {group}",
            f"mkdir -p {params.cache_dir}/optuna {params.cache_dir}/tracking/{group}",
            "",
            f"cat > {setup_path} << 'JERNERICS_EOF'",
            params.setup_command,
            "JERNERICS_EOF",
            "",
            f"cat > {trial_path} << 'JERNERICS_EOF'",
            params.trial_command,
            "JERNERICS_EOF",
            "",
            f"SETUP_ID=$(pueue add -g {group}"
            f" --label {group}_setup"
            f" -- bash {setup_path} 2>&1 | grep -oE '[0-9]+')",
        ]

        for i in range(params.n_trials):
            lines.append(
                f"TRIAL_{i + 1}_ID=$(pueue add -g {group} --after $SETUP_ID"
                f" --label {group}_trial_{i + 1}"
                f" -- bash {trial_path} 2>&1 | grep -oE '[0-9]+')"
            )

        if params.post_hook_command is not None:
            checker_inner_path = f"/tmp/jernerics_{group}_checker.sh"
            checker_wrapper_path = f"/tmp/jernerics_{group}_wait_and_check.sh"

            trial_ids = " ".join(f"$TRIAL_{i + 1}_ID" for i in range(params.n_trials))

            lines.append("")
            lines.append(f"cat > {checker_inner_path} << 'JERNERICS_EOF'")
            lines.append(params.post_hook_command)
            lines.append("JERNERICS_EOF")
            lines.append("")
            # pueue wait, not --after $TRIAL_N_ID: --after only fires on upstream
            # success, but the post-hook must run after failed trials too (it
            # detects and retries them). Costs a worker slot while waiting.
            lines.append(
                f"cat > {checker_wrapper_path} <<JERNERICS_EOF\n"
                f"pueue wait {trial_ids} -q\n"
                f"bash {checker_inner_path}\n"
                "JERNERICS_EOF"
            )
            lines.append("")

            lines.append(
                f"pueue add -g {group}"
                f" --label {group}_checker"
                f" -- bash {checker_wrapper_path}"
            )

        return "\n".join(lines)

    def render_sweep(self, params: SweepSubmissionParams) -> str:
        return self._render_script(params)

    def submit_sweep(self, params: SweepSubmissionParams) -> SubmitResult:
        script = self.render_sweep(params)

        result = self.host.run(
            ["bash"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit sweep: {result.stderr.strip()}")

        return SubmitResult(
            submissions=[
                JobSubmission(job_id=params.study_name, n_trials=params.n_trials)
            ]
        )

    def submit_job(
        self, script: str, *, name: str = "build", log_dir: str | None = None
    ) -> str:
        submission_script = (
            f"BUILD_ID=$(pueue add --label {name}"
            f" -- bash -e -c '{script}'"
            " 2>&1 | grep -oE '[0-9]+')\n"
            "echo $BUILD_ID"
        )

        result = self.host.run(
            ["bash"],
            input=submission_script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit build job: {result.stderr.strip()}")
        return result.stdout.strip()

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

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        array_index: int | None = None,
        meta: dict | None = None,
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
            output = self._get_log(job_id)
            print(output)

    def _get_log(self, task_id: str) -> str:
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

    def cleanup(self) -> None:
        self.host.run(["pueue", "clean"], check=False, capture_output=True)
