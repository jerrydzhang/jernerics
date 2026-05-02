import shlex
from pathlib import Path
from typing import Any

from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.build_marker import needs_rebuild
from jernerics.backend.command_builders import build_sweep_commands
from jernerics.backend.job_meta import save_job_meta
from jernerics.backend.models import JobInfo, SubmitResult, SweepSubmission
from jernerics.config import _normalize_time
from jernerics.retry import RetryContext


class Backend:
    def __init__(
        self,
        host,
        container,
        adapter,
        syncer,
        paths,
        *,
        remote_dir: str,
        cache_dir: str,
        project_name: str,
        tracking_server: str | None = None,
        heartbeat_interval_s: float = 60.0,
        auto_retry: bool = False,
        stale_after_s: int = 120,
        grace_period_s: int = 120,
        max_retries: int = 3,
        chain_depth_cap: int = 20,
    ):
        self.host = host
        self.container = container
        self.adapter = adapter
        self.syncer = syncer
        self.paths = paths
        self.remote_dir = remote_dir
        self.cache_dir = cache_dir
        self.project_name = project_name
        self.tracking_server = tracking_server
        self.heartbeat_interval_s = heartbeat_interval_s
        self.auto_retry = auto_retry
        self.stale_after_s = stale_after_s
        self.grace_period_s = grace_period_s
        self.max_retries = max_retries
        self.chain_depth_cap = chain_depth_cap

    def _merge_overrides(
        self,
        *,
        experiment_overrides: dict[str, Any] | None,
        cli_overrides: dict[str, str] | None,
    ) -> dict[str, str]:
        merged = {
            **{
                k: _normalize_time(v) if k == "time" else v
                for k, v in (experiment_overrides or {}).items()
            },
            **{
                k: _normalize_time(v) if k == "time" else v
                for k, v in (cli_overrides or {}).items()
            },
        }
        return {k: v for k, v in merged.items() if v is not None}

    def _build_params(
        self,
        spec: SweepSubmission,
        *,
        direction: str,
        max_parallel: int | None = None,
        overrides: dict[str, str] | None = None,
        retry_ctx_path: str | None = None,
        chain_depth: int = 0,
        multiline: bool = False,
    ) -> SweepSubmissionParams:
        wrapped_setup, wrapped_trial, post_hook = build_sweep_commands(
            spec,
            self.container,
            self.paths,
            direction=direction,
            tracking_server=self.tracking_server,
            heartbeat_interval_s=self.heartbeat_interval_s,
            multiline=multiline,
            retry_ctx_path=retry_ctx_path,
            chain_depth=chain_depth,
        )
        cache_host = self.paths.resolve_cache()
        return SweepSubmissionParams(
            setup_command=wrapped_setup,
            trial_command=wrapped_trial,
            post_hook_command=post_hook,
            n_trials=spec.n_trials,
            study_name=spec.study_name,
            log_dir=f"{cache_host}/logs",
            cache_dir=cache_host,
            max_parallel=max_parallel,
            overrides=overrides or {},
        )

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
        overrides = self._merge_overrides(
            experiment_overrides=experiment_overrides,
            cli_overrides=cli_overrides,
        )

        max_parallel_raw = overrides.pop("max_parallel", None)
        try:
            max_parallel = int(max_parallel_raw) if max_parallel_raw else None
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"max_parallel must be an integer, got: {max_parallel_raw!r}"
            ) from e

        # Pop output/error from overrides — they go into job meta, not the script
        cache_host = self.paths.resolve_cache()
        output_pattern = overrides.pop("output", None)
        error_pattern = overrides.pop("error", None)

        updated_spec = SweepSubmission(
            dag_path=spec.dag_path,
            config_path=spec.config_path,
            study_name=spec.study_name,
            storage_url=spec.storage_url,
            n_trials=spec.n_trials,
            dag_relpath=spec.dag_relpath,
            config_relpath=spec.config_relpath,
            project_name=spec.project_name,
            server_addr=spec.server_addr,
            max_parallel=max_parallel,
            backend_overrides=overrides,
            grid=spec.grid,
        )

        if dry_run:
            params = self._build_params(
                updated_spec,
                direction=direction,
                overrides=overrides,
            )
            script = self.adapter.render_sweep(params)
            print("=== DRY RUN ===")
            print(f"Backend: {backend_name}")
            print(f"Host: {getattr(self.host, 'host', 'local')}")
            print(f"Remote dir: {self.remote_dir}")
            print()
            print("=== SCRIPT ===")
            print(script)
            return None

        # Sync
        if self.syncer is not None:
            host_label = getattr(self.host, "host", "local")
            print(f"Syncing project to {host_label}:{self.remote_dir}...")
            self.syncer.sync_project(project_dir)

        # Readiness check
        cache_host = self.paths.resolve_cache()
        self.host.mkdir(f"{cache_host}/optuna")
        if self.syncer is not None:
            result = self.host.run(
                self.container.exists_command(self.remote_dir),
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                print(
                    "Error: container not found on remote.\n"
                    "  Run 'jernerics build --backend <name>' first."
                )
                raise RuntimeError("container not found on remote")

        # Retry context
        retry_ctx_path = None
        retry_ctx = None
        chain_depth = 0
        if self.auto_retry and local_cache_dir is not None:
            retry_dir_host = f"{cache_host}/retry"
            self.host.mkdir(retry_dir_host)
            retry_ctx = RetryContext(
                study_name=spec.study_name,
                backend_name=backend_name,
                dag_relpath=spec.dag_relpath,
                config_relpath=spec.config_relpath,
                cli_overrides=cli_overrides or {},
                storage_path=spec.storage_url,
                tracking_dir=self.paths.tracking_dir(spec.study_name),
                project_dir=self.paths.work_prefix,
                ctx_path=self.paths.retry_ctx_path(spec.study_name),
                chain_depth=0,
                project_name=project_name,
                host_home=self.host.home,
            )
            host_ctx_path = f"{cache_host}/retry/{spec.study_name}_ctx.json"
            self.host.write_file(host_ctx_path, retry_ctx.to_json())
            retry_ctx_path = self.paths.retry_ctx_path(spec.study_name)

        # Build params and submit
        params = self._build_params(
            updated_spec,
            direction=direction,
            max_parallel=max_parallel,
            overrides=overrides,
            retry_ctx_path=retry_ctx_path,
            chain_depth=chain_depth,
            multiline=True,
        )
        result = self.adapter.submit_sweep(params)

        # Save meta
        if local_cache_dir is not None:
            effective_output = output_pattern or f"{cache_host}/logs/%A_%a.out"
            effective_error = error_pattern or f"{cache_host}/logs/%A_%a.err"
            for sub in result.submissions:
                save_job_meta(
                    job_id=sub.job_id,
                    output_pattern=str(sub.output_pattern or effective_output),
                    error_pattern=str(sub.error_pattern or effective_error),
                    remote_dir=self.remote_dir,
                    n_trials=sub.n_trials,
                    local_cache_dir=local_cache_dir,
                )

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
            from jernerics.container.starters import generate_container_def

            container_def_path.write_text(generate_container_def("python"))
            print("Created: container.def")

        cache_host = self.paths.resolve_cache()
        marker_path = f"{cache_host}/.build_marker"

        if (
            not dry_run
            and not force
            and not needs_rebuild(self.host, marker_path, lock_path)
        ):
            print("Container is up to date. Use --force to rebuild.")
            return

        host_label = getattr(self.host, "host", None)
        if dry_run:
            print("=== DRY RUN ===")
            print(f"Project dir: {project_dir}")
            print(f"Remote dir: {self.remote_dir}")
            if host_label:
                print(f"Host: {host_label}")
            print()
            print("Would sync files and submit build job.")
            return

        self.host.mkdir(f"{cache_host}/logs")

        if self.syncer is not None:
            label = host_label or "local"
            print(f"Syncing project to {label}:{self.remote_dir}...")
            self.syncer.sync_project(project_dir)

        # Compose build script
        build_cmd = self.container.build_command(self.remote_dir)
        cmd_str = " ".join(shlex.quote(c) for c in build_cmd)
        build_dir = self.paths.resolve_build_dir(project_name)

        if build_dir is not None:
            build_script = (
                f"set -e\n"
                f"mkdir -p {build_dir}\n"
                f"export APPTAINER_TMPDIR={build_dir}\n"
                f"cd {self.remote_dir}\n"
                f"{cmd_str}\n"
                f"rm -rf {build_dir}\n"
                f"mkdir -p {Path(marker_path).parent}\n"
                f"touch {marker_path}\n"
            )
        else:
            build_script = (
                f"set -e\n"
                f"cd {self.remote_dir}\n"
                f"{cmd_str}\n"
                f"mkdir -p {Path(marker_path).parent}\n"
                f"touch {marker_path}\n"
            )

        job_id = self.adapter.submit_job(
            build_script, name="container-build", log_dir=f"{cache_host}/logs"
        )

        if job_id and local_cache_dir is not None:
            save_job_meta(
                job_id=job_id,
                output_pattern=f"{cache_host}/logs/build_%j.out",
                error_pattern=f"{cache_host}/logs/build_%j.err",
                remote_dir=self.remote_dir,
                n_trials=1,
                local_cache_dir=local_cache_dir,
            )

        print(f"Build job submitted: {job_id}")

    def clean(
        self,
        project_name: str,
        *,
        full: bool = False,
        force: bool = False,
    ) -> None:
        cache_host = self.paths.resolve_cache()

        target_desc = "cache + project directory" if full else "cache directory"
        host_label = getattr(self.host, "host", None)
        if host_label:
            print(f"Target: {target_desc} on {host_label}")
        else:
            print(f"Target: {target_desc}")
        print(f"  cache:   {cache_host}")
        if full:
            print(f"  project: {self.remote_dir}")

        active = [
            j
            for j in self.list_jobs()
            if j.status
            not in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMEOUT",
                "STASHED",
                "LOCKED",
            )
        ]
        if active:
            print(f"\nError: {len(active)} active job(s) found. Cancel them first.")
            for j in active:
                print(f"  {j.job_id}  {j.name}  {j.status}")
            raise RuntimeError("Active jobs prevent cleaning")

        result = self.host.run(
            [
                f"find {cache_host}/tracking"
                " -path '*/events/*.pb' 2>/dev/null | head -n 1"
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print("\nError: Unsynced tracking data found. Run sync first.")
            raise RuntimeError("Unsynced tracking data")

        # Check for unsynced artifact manifests
        result = self.host.run(
            [
                f"cd {cache_host}/tracking && "
                "for m in $(find . -path '*/artifacts/*.manifest' 2>/dev/null); do "
                'c="${m%.manifest}.cursor"; '
                'ms=$(stat -c%s "$m" 2>/dev/null || echo 0); '
                'cs=$(cat "$c" 2>/dev/null || echo 0); '
                'if [ "$cs" -lt "$ms" ]; then echo "$m"; break; fi; '
                "done"
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print("\nError: Unsynced artifact data found. Run sync first.")
            raise RuntimeError("Unsynced artifact data")

        r = self.host.run(["test", "-d", cache_host], check=False, capture_output=True)
        if r.returncode != 0:
            print(f"\nError: cache directory '{cache_host}' not found.")
            raise FileNotFoundError(f"Cache directory not found: {cache_host}")

        if full:
            r = self.host.run(
                ["test", "-d", self.remote_dir], check=False, capture_output=True
            )
            if r.returncode != 0:
                print(f"\nError: project directory '{self.remote_dir}' not found.")
                raise FileNotFoundError(
                    f"Project directory not found: {self.remote_dir}"
                )

        if not force:
            print("\nDry run. Use --force to execute.")
            return

        self.adapter.cleanup()

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

        cache_host = self.paths.resolve_cache()
        bind_args = self.paths.bind_args(cache_host)

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
        self.adapter.get_logs(
            job_id,
            follow=follow,
            stderr=stderr,
            meta={"local_cache_dir": local_cache_dir, "host": self.host},
        )

    # Delegated to adapter

    def list_jobs(self, include_completed: bool = False) -> list[JobInfo]:
        return self.adapter.list_jobs(include_completed=include_completed)

    def cancel(self, job_id: str) -> bool:
        return self.adapter.cancel(job_id)

    def cancel_all(self) -> bool:
        return self.adapter.cancel_all()

    def get_status(self, job_id: str) -> str | None:
        return self.adapter.get_status(job_id)

    def wait_for_completion(
        self, job_id: str, poll_interval: float = 30, timeout: float | None = None
    ) -> bool:
        return self.adapter.wait_for_completion(job_id, poll_interval, timeout)

    def storage_path(self, study_name: str) -> str:
        return self.paths.storage_path(study_name)

    def cleanup(self) -> None:
        self.adapter.cleanup()
