import json
import time
from pathlib import Path
from typing import Any

from jernerics.backend.components.container import NoContainer
from jernerics.backend.models import JobInfo, SubmitResult, SweepSpec
from jernerics.config import BackendConfig, PueueConfig
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

    @classmethod
    def from_config(
        cls,
        backend_config: BackendConfig,
        *,
        host,
        syncer=None,
        tracking_server: str | None = None,
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
            container = Docker(host)
        elif container_type == "apptainer":
            container = Apptainer(host)
        elif container_type == "none":
            container = NoContainer()
        else:
            container = Docker(host)

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
        )

    @property
    def _work_prefix(self) -> str:
        if isinstance(self.container, NoContainer):
            return self.remote_dir
        return "/work"

    @property
    def _cache_prefix(self) -> str:
        if isinstance(self.container, NoContainer):
            return self.cache_dir.replace("~", "$HOME")
        return "/cache"

    def storage_path(self, study_name: str, project_name: str) -> str:
        if isinstance(self.container, NoContainer):
            cache = self._resolve_cache(project_name)
            return f"{cache}/optuna/{study_name}.journal"
        return f"/cache/optuna/{study_name}.journal"

    def _resolve_cache(self, project_name: str) -> str:
        cache = self.cache_dir
        template = "project_name"
        if "{" + template + "}" in cache:
            cache = cache.replace("{" + template + "}", project_name)
        elif project_name:
            cache = f"{cache}/{project_name}"
        return cache.replace("~", "$HOME")

    def _bind_args(self, cache_host: str) -> list[str]:
        return [
            f"{self.remote_dir}:/work",
            f"{cache_host}:/cache",
        ]

    def _build_setup_command(
        self,
        study_name: str,
        storage_path: str,
        direction: str,
        config_relpath: str = "",
        grid: dict[str, list] | None = None,
        work_prefix: str = "/work",
        cache_prefix: str = "/cache",
    ) -> str:
        sampler_expr = "None"
        if config_relpath:
            sampler_expr = (
                f"__import__('jernerics.config', fromlist=['load_config'])"
                f".load_config('{work_prefix}/{config_relpath}').sampler"
            )

        lines = [
            'python -c "',
            "from optuna.storages.journal import JournalFileBackend, JournalStorage; ",
            "import optuna, itertools, json; ",
            f"sampler = {sampler_expr}; ",
            "study = optuna.create_study(",
            f"study_name={study_name!r},",
            f" storage=JournalStorage(JournalFileBackend({storage_path!r})),",
            f" direction={direction!r},",
            " sampler=sampler,",
            " load_if_exists=True);",
        ]

        if grid:
            import base64

            grid_b64 = base64.b64encode(json.dumps(grid).encode()).decode()
            lines.append(
                "import base64, os; "
                f"_sentinel = '{cache_prefix}/optuna/{study_name}.grid_enqueued';"
                f" grid = json.loads(base64.b64decode({grid_b64!r}));"
                f" keys = sorted(grid.keys());"
                f" [study.enqueue_trial(dict(zip(keys, combo, strict=True)))"
                f" for combo in itertools.product(*[grid[k] for k in keys])"
                " if not os.path.exists(_sentinel)];"
                f" os.makedirs('{cache_prefix}/optuna', exist_ok=True);"
                f" open(_sentinel, 'a').close();"
            )

        lines.append('"')
        return "".join(lines)

    def _build_trial_command(
        self,
        dag_relpath: str,
        config_relpath: str,
        study_name: str,
        storage_path: str,
        project_name: str | None,
        tracking_dir: str,
        tracking_server: str | None,
        work_prefix: str = "/work",
    ) -> str:
        args = [
            "python",
            "-m",
            "jernerics.runner",
            f"{work_prefix}/{dag_relpath}",
            f"{work_prefix}/{config_relpath}",
            "--study-name",
            study_name,
            "--storage-url",
            storage_path,
            "--tracking-dir",
            tracking_dir,
        ]
        if project_name:
            args.extend(["--project-name", project_name])
        if tracking_server:
            args.extend(["--server-addr", tracking_server])
        return " ".join(args)

    def _build_checker_command(
        self,
        ctx_path: str,
        chain_depth: int,
    ) -> str:
        args = [
            "python",
            "-m",
            "jernerics.retry_checker",
            "--context",
            ctx_path,
            "--chain-depth",
            str(chain_depth),
        ]
        return " ".join(args)

    def _generate_submit_script(
        self,
        spec: SweepSpec,
        *,
        direction: str = "minimize",
        retry_ctx: RetryContext | None = None,
    ) -> str:
        project_name = spec.project_name or ""
        cache_host = self._resolve_cache(project_name)
        bind_args = self._bind_args(cache_host)
        tracking_dir = f"{cache_host}/tracking/{spec.study_name}"

        dag_relpath = spec.dag_relpath or str(spec.dag_path.name)
        config_relpath = spec.config_relpath or str(spec.config_path.name)

        setup_cmd = self._build_setup_command(
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            direction=direction,
            config_relpath=config_relpath,
            grid=spec.grid,
            work_prefix=self._work_prefix,
            cache_prefix=self._cache_prefix,
        )
        wrapped_setup = self.container.wrap(setup_cmd, bind_args)

        trial_cmd = self._build_trial_command(
            dag_relpath=dag_relpath,
            config_relpath=config_relpath,
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            project_name=spec.project_name,
            tracking_dir=tracking_dir,
            tracking_server=self.tracking_server,
            work_prefix=self._work_prefix,
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
            checker_cmd = self._build_checker_command(
                retry_ctx.ctx_path, retry_ctx.chain_depth
            )
            wrapped_checker = self.container.wrap(
                f"{checker_cmd} 2>/dev/null", bind_args
            )
            wrapped_checker += " | bash"

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
        spec: SweepSpec,
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

    @staticmethod
    def _save_job_meta(
        job_id: str,
        remote_dir: str,
        n_trials: int,
        local_cache_dir: Path,
    ) -> None:
        job_meta = {
            "job_id": job_id,
            "backend": "pueue",
            "remote_dir": remote_dir,
            "n_trials": n_trials,
        }
        meta_dir = local_cache_dir / "jobs"
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_file = meta_dir / f"{job_id}.json"
        meta_file.write_text(json.dumps(job_meta, indent=2))

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
    ) -> SubmitResult | None:
        if dry_run:
            print("=== DRY RUN ===")
            print(f"Backend: {backend_name} (pueue)")
            print(f"Group: {spec.study_name}")
            print(f"Trials: {spec.n_trials}")
            return None

        if self.syncer is not None:
            print(f"Syncing project to {self.host.host}:{self.remote_dir}...")
            self.syncer.sync_project(project_dir)

        if self.auto_retry and local_cache_dir is not None:
            project_name_val = project_name or ""
            cache_host = self._resolve_cache(project_name_val)

            if isinstance(self.container, NoContainer):
                cache_host = cache_host.replace("$HOME", str(Path.home()))
                retry_dir_host = f"{cache_host}/retry"
                self.host.mkdir(retry_dir_host)
                checker_ctx_path = f"{retry_dir_host}/{spec.study_name}_ctx.json"
                tracking_dir_ctx = f"{cache_host}/tracking/{spec.study_name}"
                project_dir_ctx = str(Path(self.remote_dir).resolve())
                storage_url_ctx = spec.storage_url.replace("$HOME", str(Path.home()))
            else:
                retry_dir_host = f"{cache_host}/retry"
                self.host.mkdir(retry_dir_host)
                checker_ctx_path = f"/cache/retry/{spec.study_name}_ctx.json"
                tracking_dir_ctx = f"/cache/tracking/{spec.study_name}"
                project_dir_ctx = "/work"
                storage_url_ctx = spec.storage_url

            ctx = RetryContext(
                study_name=spec.study_name,
                backend_name=backend_name,
                dag_relpath=spec.dag_relpath,
                config_relpath=spec.config_relpath,
                cli_overrides=cli_overrides or {},
                storage_path=storage_url_ctx,
                tracking_dir=tracking_dir_ctx,
                project_dir=project_dir_ctx,
                ctx_path=checker_ctx_path,
                chain_depth=0,
            )
            host_ctx_path = f"{retry_dir_host}/{spec.study_name}_ctx.json"
            self.host.write_file(host_ctx_path, ctx.to_json())

            result = self.submit_sweep(spec, direction=direction, retry_ctx=ctx)
        else:
            result = self.submit_sweep(spec, direction=direction)

        if local_cache_dir is not None:
            self._save_job_meta(
                job_id=result.job_id,
                remote_dir=self.remote_dir,
                n_trials=spec.n_trials,
                local_cache_dir=local_cache_dir,
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
        lock_path = project_dir / "uv.lock"
        if not lock_path.exists():
            raise FileNotFoundError("uv.lock not found. Run 'uv lock' first.")

        container_def_path = project_dir / "container.def"
        dockerfile_path = project_dir / "Dockerfile"
        has_build_file = container_def_path.exists() or dockerfile_path.exists()

        if not has_build_file:
            from jernerics.container.templates import generate_container_def

            container_def_path.write_text(generate_container_def("python"))
            print("Created: container.def")

        if not dry_run and not force:
            needs_rebuild = (
                self.syncer is not None
                and self.syncer.container_needs_rebuild(lock_path)
            )
            if not needs_rebuild and self.container.exists(self.remote_dir):
                print("Container is up to date. Use --force to rebuild.")
                return

        if dry_run:
            print("=== DRY RUN ===")
            print(f"Project dir: {project_dir}")
            print(f"Remote dir: {self.remote_dir}")
            if hasattr(self.host, "host"):
                print(f"Host: {self.host.host}")
            print()
            print("Would sync files and build container.")
            return

        if self.syncer is not None:
            print(f"[1/2] Syncing project to {self.host.host}:{self.remote_dir}...")
            self.syncer.sync_project(project_dir)
        else:
            print("[1/2] Local build, no sync needed.")

        print("[2/2] Building container...")
        self.container.build(self.remote_dir)
        print("Build complete.")

    def clean(
        self,
        project_name: str,
        *,
        full: bool = False,
        force: bool = False,
    ) -> None:
        cache_host = self._resolve_cache(project_name)

        target_desc = "cache + project directory" if full else "cache directory"
        if hasattr(self.host, "host"):
            print(f"Target: {target_desc} on {self.host.host}")
        else:
            print(f"Target: {target_desc}")
        print(f"  cache:   {cache_host}")
        if full:
            print(f"  project: {self.remote_dir}")

        try:
            data = _query_pueue_status(self.host)
            active = [
                j
                for j in _parse_pueue_status(data)
                if j.status not in ("COMPLETED", "FAILED", "STASHED", "LOCKED")
            ]
        except PueueDaemonError:
            active = []

        if active:
            print(f"\nError: {len(active)} active job(s). Cancel them first.")
            for j in active:
                print(f"  {j.job_id}  {j.name}  {j.status}")
            raise RuntimeError("Active jobs prevent cleaning")

        result = self.host.run(
            [f"find {cache_host}/tracking -name '*.pb' 2>/dev/null | head -n 1"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print("\nError: Unsynced tracking data found. Run sync first.")
            raise RuntimeError("Unsynced tracking data")

        r = self.host.run(["test", "-d", cache_host], check=False, capture_output=True)
        if r.returncode != 0:
            print(f"\nError: cache directory '{cache_host}' not found.")
            raise FileNotFoundError(f"Cache directory not found: {cache_host}")

        if full:
            r = self.host.run(
                ["test", "-d", self.remote_dir],
                check=False,
                capture_output=True,
            )
            if r.returncode != 0:
                print(f"\nError: project directory '{self.remote_dir}' not found.")
                raise FileNotFoundError(
                    f"Project directory not found: {self.remote_dir}"
                )

        if not force:
            print("\nDry run. Use --force to execute.")
            return

        self.host.run(["pueue", "clean"], check=False, capture_output=True)

        r = self.host.run(
            ["rm", "-rf", cache_host], check=False, capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f"Failed to delete {cache_host}: {r.stderr}")
            raise RuntimeError(f"Failed to delete {cache_host}")
        print(f"Deleted: {cache_host}")

        if full:
            r = self.host.run(
                ["rm", "-rf", self.remote_dir],
                check=False,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                print(f"Failed to delete {self.remote_dir}: {r.stderr}")
                raise RuntimeError(f"Failed to delete {self.remote_dir}")
            print(f"Deleted: {self.remote_dir}")

    def sync(
        self,
        project_name: str,
        *,
        study: str | None = None,
    ) -> None:
        if not self.tracking_server:
            raise RuntimeError("No tracking server configured")

        import shlex

        cache_host = self._resolve_cache(project_name)
        bind_args = self._bind_args(cache_host)

        inner_cmd = (
            "python -m jernerics.tracking.replay_runner"
            " --tracking-dir /cache/tracking"
            f" --server-addr {self.tracking_server}"
        )
        if study:
            inner_cmd += f" --study {shlex.quote(study)}"

        wrapped = self.container.wrap(inner_cmd, bind_args)
        cmd = f"cd {self.remote_dir} && {wrapped}"

        host_desc = getattr(self.host, "host", "local")
        print(f"Syncing tracking data from {host_desc}...")
        result = self.host.run([cmd], check=False, capture_output=True, text=True)
        print(result.stdout)
        if result.returncode != 0:
            print(f"Sync failed: {result.stderr}")
            raise RuntimeError(f"Sync failed: {result.stderr}")
        print("Sync complete.")

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
