import json
import os
import re
import time
from pathlib import Path
from typing import Any

from jernerics.backend.components.command_builders import (
    build_checker_command,
    build_setup_command,
    build_trial_command,
)
from jernerics.backend.components.container import (
    Apptainer,
    Docker,
    NoContainer,
)
from jernerics.backend.components.job_meta import save_job_meta
from jernerics.backend.components.path_resolver import PathResolver
from jernerics.backend.models import JobInfo, SubmitResult, SweepSubmission
from jernerics.config import (
    ApptainerConfig,
    BackendConfig,
    SlurmConfig,
    _normalize_time,
)
from jernerics.retry import RetryContext

_SLURM_VALUE_PATTERN = re.compile(r"^[a-zA-Z0-9_.:/\-]+$")


def _validate_slurm_value(value: str, name: str) -> str:
    if not _SLURM_VALUE_PATTERN.match(value):
        raise ValueError(
            f"Invalid {name} value '{value}': contains disallowed characters. "
            "Only alphanumeric, underscore, hyphen, period, colon, and slash allowed."
        )
    return value


def expand_slurm_pattern(
    pattern: str,
    job_id: str | None = None,
    array_task_id: str | int | None = None,
    job_name: str | None = None,
    replace_unknown_with_wildcard: bool = False,
) -> str:
    result = pattern

    if job_id is not None:
        base_job_id = job_id.split("_")[0] if "_" in job_id else job_id
        result = result.replace("%j", job_id)
        result = result.replace("%A", base_job_id)

    if array_task_id is not None:
        result = result.replace("%a", str(array_task_id))
    elif replace_unknown_with_wildcard and "%a" in result:
        result = result.replace("%a", "*")

    if job_name is not None:
        result = result.replace("%x", job_name)
    elif replace_unknown_with_wildcard and "%x" in result:
        result = result.replace("%x", "*")

    result = result.replace("%u", os.environ.get("USER", "unknown"))

    if replace_unknown_with_wildcard:
        result = result.replace("%N", "*")

    return result


class SlurmBackend:
    def __init__(
        self,
        host,
        container,
        syncer,
        *,
        remote_dir: str,
        partition: str = "priority",
        time: str | None = "1:00:00",
        mem: str = "16G",
        cpus: int = 4,
        max_concurrent_jobs: int = 10,
        cache_dir: str | None = None,
        tracking_server: str | None = None,
        heartbeat_interval_s: float = 60.0,
        auto_retry: bool = False,
        stale_after_s: int = 120,
        grace_period_s: int = 120,
        max_retries: int = 3,
        chain_depth_cap: int = 20,
        build_dir: str | None = None,
    ):
        self.host = host
        self.container = container
        self.syncer = syncer
        self.remote_dir = remote_dir
        self.partition = partition
        self.time = time
        self.mem = mem
        self.cpus = cpus
        self.max_concurrent_jobs = max_concurrent_jobs
        self.cache_dir = cache_dir
        self.tracking_server = tracking_server
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
            work_mount_source="${REMOTE_DIR}",
            quote_binds=True,
            build_dir=build_dir,
        )

    capabilities = frozenset()

    def generate_submit_job(
        self,
        script: str,
        *,
        name: str = "build",
        log_dir: str | None = None,
    ) -> str:
        partition = _validate_slurm_value(self.partition, "partition")
        time_val = _validate_slurm_value(self.time or "1:00:00", "time")
        mem = _validate_slurm_value(self.mem, "mem")
        cpus = _validate_slurm_value(str(self.cpus), "cpus")

        sbatch_script = (
            "#!/bin/bash\n"
            f"#SBATCH --job-name={name}\n"
            f"#SBATCH --partition={partition}\n"
            f"#SBATCH --time={time_val}\n"
            f"#SBATCH --mem={mem}\n"
            f"#SBATCH --cpus-per-task={cpus}\n"
        )
        if log_dir is not None:
            expanded = _expand_path(log_dir)
            sbatch_script += (
                f"#SBATCH --output={expanded}/build_%j.out\n"
                f"#SBATCH --error={expanded}/build_%j.err\n"
            )
        sbatch_script += "\n" + script

        lines = [
            "BUILD_JOB_ID=$(sbatch --parsable <<'JERNERICS_EOF'",
            sbatch_script,
            "JERNERICS_EOF",
            ")",
            "echo $BUILD_JOB_ID",
        ]
        return "\n".join(lines)

    @classmethod
    def from_config(
        cls,
        backend_config: BackendConfig,
        *,
        host=None,
        syncer=None,
        tracking_server: str | None = None,
    ) -> "SlurmBackend":
        """Construct from config.

        `host` must be provided explicitly. Use StdoutHost() when composing
        a bash script for piping (e.g. retry checker inside a container).
        """

        container_type = backend_config.shared.container_type
        if container_type == "apptainer":
            container = Apptainer()
        elif container_type == "docker":
            container = Docker()
        elif container_type == "none":
            container = NoContainer()
        else:
            container = Apptainer()

        assert isinstance(backend_config.backend, SlurmConfig)
        slurm = backend_config.backend

        build_dir = None
        if isinstance(backend_config.container, ApptainerConfig):
            build_dir = backend_config.container.build_dir

        return cls(
            host=host,
            container=container,
            syncer=syncer,
            remote_dir=backend_config.shared.remote_dir.replace("~", "$HOME"),
            partition=slurm.partition,
            time=slurm.time,
            mem=slurm.mem,
            cpus=slurm.cpus,
            max_concurrent_jobs=slurm.max_concurrent_jobs,
            cache_dir=backend_config.shared.cache_dir,
            tracking_server=tracking_server,
            heartbeat_interval_s=backend_config.shared.heartbeat_interval_s,
            auto_retry=backend_config.shared.auto_retry,
            stale_after_s=backend_config.shared.stale_after_s,
            grace_period_s=backend_config.shared.grace_period_s,
            max_retries=backend_config.shared.max_retries,
            chain_depth_cap=backend_config.shared.chain_depth_cap,
            build_dir=build_dir,
        )

    def storage_path(self, study_name: str, project_name: str) -> str:
        return self._paths.storage_path(study_name, project_name)

    def _resolve_output_dir(self, output_path: str) -> str:
        if "%" in output_path:
            before_pattern = output_path[: output_path.index("%")]
            last_slash = before_pattern.rfind("/")
            return before_pattern[:last_slash] if last_slash >= 0 else "."
        from pathlib import Path

        return str(Path(output_path).parent)

    def _expand_path(self, p: str) -> str:
        if p.startswith("~"):
            return "$HOME" + p[1:]
        return p

    def _build_array_script(
        self,
        spec: SweepSubmission,
        direction: str,
    ) -> str:
        """Generate the SLURM array job script."""
        max_parallel_val = spec.max_parallel or self.max_concurrent_jobs
        if max_parallel_val > 0:
            array_spec = f"1-{spec.n_trials}%{max_parallel_val}"
        else:
            array_spec = f"1-{spec.n_trials}"

        dag_relpath = spec.dag_relpath or str(spec.dag_path.name)
        config_relpath = spec.config_relpath or str(spec.config_path.name)
        project_name = spec.project_name or ""
        cache_host = self._paths.resolve_cache(project_name)
        tracking_dir = self._paths.tracking_dir(spec.study_name)

        setup_command = build_setup_command(
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            direction=direction,
            config_relpath=config_relpath,
            grid=spec.grid,
            work_prefix=self._paths.work_prefix,
            cache_prefix=self._paths.cache_prefix,
        )
        trial_command = build_trial_command(
            dag_relpath=dag_relpath,
            config_relpath=config_relpath,
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            project_name=spec.project_name,
            tracking_dir=tracking_dir,
            tracking_server=self.tracking_server,
            heartbeat_interval_s=self.heartbeat_interval_s,
            work_prefix=self._paths.work_prefix,
            multiline=True,
        )

        return self._generate_sweep_script(
            setup_command=setup_command,
            trial_command=trial_command,
            array_spec=array_spec,
            study_name=spec.study_name,
            project_name=spec.project_name or "",
            backend_overrides=spec.backend_overrides,
        )

    def _build_checker_script(
        self,
        ctx_path: str,
        chain_depth: int,
        cache_host: str,
        partition: str,
        dependency_job_id: str | None = None,
    ) -> str:
        """Generate the SLURM checker job script."""
        checker_cmd = build_checker_command(ctx_path, chain_depth)
        wrapped_checker = self.container.wrap(
            f"{checker_cmd} 2>/dev/null", self._paths.bind_args(cache_host)
        )
        wrapped_checker += " | bash"

        return _format_checker_script(
            cache_host=cache_host,
            remote_dir=self.remote_dir,
            partition=partition,
            wrapped_checker=wrapped_checker,
            dependency_job_id=dependency_job_id,
        )

    def submit_sweep(
        self,
        spec: SweepSubmission,
        *,
        direction: str = "minimize",
        retry_ctx: RetryContext | None = None,
    ) -> SubmitResult:
        array_script = self._build_array_script(spec, direction)

        if retry_ctx is None:
            result = self.host.run(
                [f"cd {self.remote_dir} && sbatch --parsable"],
                input=array_script,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to submit job: {result.stderr.strip()}")
            return SubmitResult(job_id=result.stdout.strip())

        project_name = spec.project_name or ""
        cache_host = self._paths.resolve_cache(project_name)
        partition = spec.backend_overrides.get("partition", self.partition)

        checker_script = self._build_checker_script(
            ctx_path=retry_ctx.ctx_path,
            chain_depth=retry_ctx.chain_depth,
            cache_host=cache_host,
            partition=partition,
        )

        combined = _compose_chain(array_script, checker_script)

        result = self.host.run(
            ["bash"],
            input=combined,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit job chain: {result.stderr.strip()}")

        parts = result.stdout.strip().split(" ", 1)
        job_id = parts[0]
        checker_id = parts[1] if len(parts) > 1 else None
        return SubmitResult(job_id=job_id, checker_job_id=checker_id)

    def _merge_overrides(
        self,
        *,
        experiment_overrides: dict[str, Any] | None,
        cli_overrides: dict[str, str] | None,
    ) -> dict[str, str]:
        merged = {
            **self._slurm_defaults(),
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

    def _slurm_defaults(self) -> dict[str, str | None]:
        return {
            "partition": self.partition,
            "time": self.time,
            "mem": self.mem,
        }

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
        merged = self._merge_overrides(
            experiment_overrides=experiment_overrides,
            cli_overrides=cli_overrides,
        )

        max_parallel = merged.pop("max_parallel", self.max_concurrent_jobs)
        try:
            max_parallel_val = int(max_parallel) if max_parallel else 0
        except (ValueError, TypeError) as e:
            raise ValueError(
                f"max_parallel must be an integer, got: {max_parallel!r}"
            ) from e

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
            max_parallel=max_parallel_val or None,
            backend_overrides=merged,
            grid=spec.grid,
        )

        cache_host = self._paths.resolve_cache(project_name)
        output_pattern = merged.get("output", f"{cache_host}/logs/%A_%a.out")
        error_pattern = merged.get("error", f"{cache_host}/logs/%A_%a.err")

        if dry_run:
            print("=== DRY RUN ===")
            print(f"Backend: {backend_name}")
            print(f"Host: {self.host.host}")
            print(f"Remote dir: {self.remote_dir}")
            print()
            print("=== SLURM SCRIPT ===")
            print(
                self._generate_sweep_script(
                    setup_command=build_setup_command(
                        study_name=spec.study_name,
                        storage_path=spec.storage_url,
                        direction=direction,
                        config_relpath=spec.config_relpath,
                        grid=spec.grid,
                        work_prefix=self._paths.work_prefix,
                        cache_prefix=self._paths.cache_prefix,
                    ),
                    trial_command=build_trial_command(
                        dag_relpath=spec.dag_relpath,
                        config_relpath=spec.config_relpath,
                        study_name=spec.study_name,
                        storage_path=spec.storage_url,
                        project_name=spec.project_name,
                        tracking_dir=self._paths.tracking_dir(spec.study_name),
                        tracking_server=self.tracking_server,
                        work_prefix=self._paths.work_prefix,
                        multiline=True,
                    ),
                    array_spec=f"1-{spec.n_trials}"
                    + (f"%{max_parallel_val}" if max_parallel_val > 0 else ""),
                    study_name=spec.study_name,
                    project_name=project_name,
                    backend_overrides=merged,
                )
            )
            return None

        from jernerics.backend.orchestration import prepare_and_submit

        def ensure_ready():
            self.host.mkdir(f"{cache_host}/optuna")

            if not self.syncer.container_exists():
                print(
                    "Error: container.sif not found on remote.\n"
                    "  Run 'jernerics build --backend <name>' first."
                )
                raise RuntimeError("container.sif not found on remote")

        def do_submit(
            sweep_spec: SweepSubmission,
            direction: str,
            retry_ctx=None,
        ) -> SubmitResult:
            return self.submit_sweep(
                sweep_spec, direction=direction, retry_ctx=retry_ctx
            )

        def save_meta(result: SubmitResult) -> None:
            if local_cache_dir is None:
                return
            if self.auto_retry:
                save_job_meta(
                    job_id=result.job_id,
                    output_pattern=str(output_pattern),
                    error_pattern=str(error_pattern),
                    remote_dir=self.remote_dir,
                    n_trials=spec.n_trials,
                    local_cache_dir=local_cache_dir,
                )
                if result.checker_job_id:
                    save_job_meta(
                        job_id=result.checker_job_id,
                        output_pattern=f"{cache_host}/logs/checker_%j.out",
                        error_pattern=f"{cache_host}/logs/checker_%j.err",
                        remote_dir=self.remote_dir,
                        n_trials=0,
                        local_cache_dir=local_cache_dir,
                    )
            else:
                save_job_meta(
                    job_id=result.job_id,
                    output_pattern=str(output_pattern),
                    error_pattern=str(error_pattern),
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
            spec=updated_spec,
            project_dir=project_dir,
            project_name=project_name,
            direction=direction,
            backend_name=backend_name,
            auto_retry=self.auto_retry,
            local_cache_dir=local_cache_dir,
            cli_overrides=cli_overrides,
            ensure_submission_ready=ensure_ready,
            submit_sweep=do_submit,
            save_meta=save_meta,
        )

        has_retry = self.auto_retry and local_cache_dir is not None
        if has_retry and result.checker_job_id:
            print(f"\nArray: {result.job_id}, Checker: {result.checker_job_id}")
        else:
            print(f"\nJob submitted: {result.job_id}")
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

        cache_host = self._paths.resolve_cache(project_name)
        self.host.mkdir(f"{cache_host}/logs")

        job_id = submit_build(
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

        if job_id and local_cache_dir is not None:
            save_job_meta(
                job_id=job_id,
                output_pattern=f"{cache_host}/logs/build_%j.out",
                error_pattern=f"{cache_host}/logs/build_%j.err",
                remote_dir=self.remote_dir,
                n_trials=1,
                local_cache_dir=local_cache_dir,
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
            return [
                j
                for j in self.list_jobs()
                if j.status not in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT")
            ]

        shared_clean(
            host=self.host,
            paths=self._paths,
            remote_dir=self.remote_dir,
            project_name=project_name,
            full=full,
            force=force,
            list_active_jobs=list_active,
            scheduler_cleanup=lambda: None,
        )

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        local_cache_dir: Path | None = None,
    ) -> None:
        import subprocess as sp

        from jernerics.config import ExitCode

        if local_cache_dir is not None:
            meta_file = local_cache_dir / "jobs" / f"{job_id}.json"
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                output_pattern = meta.get("output_pattern", "logs/slurm_%j.out")
                error_pattern = meta.get("error_pattern", "logs/slurm_%j.err")
                meta_remote_dir = meta.get("remote_dir", self.remote_dir)
                n_trials = meta.get("n_trials", 1)
            else:
                output_pattern = None
                error_pattern = None
                meta_remote_dir = self.remote_dir
                n_trials = 1
        else:
            output_pattern = None
            error_pattern = None
            meta_remote_dir = self.remote_dir
            n_trials = 1

        if output_pattern is None or error_pattern is None:
            cache_host = self._paths.resolve_cache("")
            output_pattern = f"{cache_host}/logs/%A_%a.out"
            error_pattern = f"{cache_host}/logs/%A_%a.err"

        log_pattern = error_pattern if stderr else output_pattern

        base_job_id = job_id.split("_")[0] if "_" in job_id else job_id
        array_idx = job_id.split("_")[1] if "_" in job_id else None

        effective_array_index = array_idx
        if effective_array_index is None and n_trials == 1:
            effective_array_index = 1

        log_file = self.resolve_log_path(
            log_pattern,
            job_id=job_id,
            array_task_id=effective_array_index,
            replace_unknown_with_wildcard=True,
        )

        if not log_file.startswith("/") and not log_file.startswith("~"):
            log_file = f"{meta_remote_dir}/{log_file}"

        max_retries = 5
        retry_delay = 1.0

        if "*" in log_file:
            is_array_pattern = "%a" in log_pattern and effective_array_index is None
            if follow and is_array_pattern:
                print("Error: --follow requires --array-index for array jobs")
                raise SystemExit(ExitCode.GENERAL_ERROR)
            for attempt in range(max_retries):
                result = self.host.run(
                    [f"cat {log_file}"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(result.stdout)
                    return
                if attempt == 0:
                    print("Waiting for logs...")
                time.sleep(retry_delay)
            print(f"Error: Log files not found: {log_file}")
            raise SystemExit(ExitCode.GENERAL_ERROR)
        elif follow:
            for attempt in range(max_retries):
                result = self.host.run([f"test -f {log_file}"], check=False)
                if result.returncode == 0:
                    break
                if attempt == 0:
                    print("Waiting for logs...")
                time.sleep(retry_delay)
            else:
                print(f"Error: Log file not found: {log_file}")
                raise SystemExit(ExitCode.GENERAL_ERROR)

            if effective_array_index is not None:
                status_job_id = f"{base_job_id}_{effective_array_index}"
            else:
                status_job_id = base_job_id

            tail_proc = sp.Popen(["ssh", self.host.host, "tail", "-f", log_file])
            try:
                self.wait_for_completion(status_job_id, poll_interval=10)
            except KeyboardInterrupt:
                pass
            finally:
                tail_proc.terminate()
                tail_proc.wait()
        else:
            for attempt in range(max_retries):
                result = self.host.run(
                    [f"cat {log_file}"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(result.stdout)
                    return
                if attempt == 0:
                    print("Waiting for logs...")
                time.sleep(retry_delay)
            print(f"Error: Log file not found: {log_file}")
            raise SystemExit(ExitCode.GENERAL_ERROR)

    def _generate_sweep_script(
        self,
        setup_command: str,
        trial_command: str,
        array_spec: str,
        study_name: str,
        project_name: str,
        backend_overrides: dict[str, str],
    ) -> str:
        cache_host = self._paths.resolve_cache(project_name)
        bind_args = self._paths.bind_args(cache_host)
        wrapped_setup = self.container.wrap(setup_command, bind_args)
        wrapped_trial = self.container.wrap(trial_command, bind_args)

        slurm_opts = {**backend_overrides}
        output_pattern = slurm_opts.pop("output", None)
        error_pattern = slurm_opts.pop("error", None)
        if output_pattern:
            slurm_opts["output"] = self._expand_path(output_pattern)
        if error_pattern:
            slurm_opts["error"] = self._expand_path(error_pattern)

        output_dir = self._resolve_output_dir(
            slurm_opts.get("output", f"{cache_host}/logs/%A_%a.out")
        )

        return _format_sweep_script(
            array_spec=array_spec,
            study_name=study_name,
            cache_host=cache_host,
            remote_dir=self.remote_dir,
            partition=self.partition,
            time=self.time,
            mem=self.mem,
            backend_overrides=slurm_opts,
            wrapped_setup=wrapped_setup,
            wrapped_trial=wrapped_trial,
            output_dir=output_dir,
        )

    def list_jobs(self, include_completed: bool = False) -> list[JobInfo]:
        fmt = "%i|%j|%T"
        result = self.host.run(
            [f"squeue -u $USER -o '{fmt}' 2>/dev/null || echo ''"],
            check=False,
            capture_output=True,
            text=True,
        )

        jobs: list[JobInfo] = []
        seen_ids: set[str] = set()
        for line in result.stdout.strip().split("\n")[1:]:
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                seen_ids.add(parts[0])
                jobs.append(JobInfo(job_id=parts[0], name=parts[1], status=parts[2]))

        if include_completed:
            sacct_fmt = "JobID%20,JobName%50,State%15"
            sacct_result = self.host.run(
                [
                    f"sacct -u $USER --starttime $(date -d '1 day ago' +%Y-%m-%d) "
                    f"--format={sacct_fmt}"
                    f" --noheader --parsable2 2>/dev/null || echo ''"
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            for line in sacct_result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("|")
                if len(parts) >= 3:
                    job_id = parts[0].strip()
                    if job_id not in seen_ids:
                        seen_ids.add(job_id)
                        jobs.append(
                            JobInfo(
                                job_id=job_id,
                                name=parts[1].strip(),
                                status=parts[2].strip(),
                            )
                        )

        return jobs

    def cancel(self, job_id: str) -> bool:
        result = self.host.run(["scancel", job_id], check=False)
        return result.returncode == 0

    def cancel_all(self) -> bool:
        result = self.host.run(
            ["scancel", "-u", "$USER"],
            check=False,
        )
        return result.returncode == 0

    def get_status(self, job_id: str) -> str | None:
        result = self.host.run(
            [f"squeue -j {job_id} -o '%T' -h"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        sacct_result = self.host.run(
            [f"sacct -j {job_id} --format=State --noheader --parsable2"],
            check=False,
            capture_output=True,
            text=True,
        )
        if sacct_result.returncode == 0 and sacct_result.stdout.strip():
            states = [
                line.strip()
                for line in sacct_result.stdout.strip().split("\n")
                if line.strip()
            ]
            if states:
                main_state = states[0].split("+")[0].split(":")[0]
                return main_state
        return None

    def wait_for_completion(
        self, job_id: str, poll_interval: float = 30, timeout: float | None = None
    ) -> bool:
        start_time = time.time()
        terminal_states = {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "NODE_FAIL",
            "OUT_OF_MEMORY",
            "PREEMPTED",
            "BOOT_FAIL",
            "DEADLINE",
            "LAUNCH_FAILED",
        }
        while True:
            if timeout is not None and (time.time() - start_time) >= timeout:
                raise TimeoutError(
                    f"Timeout waiting for job {job_id} after {timeout} seconds"
                )
            status = self.get_status(job_id)
            if status is None:
                return True
            if status in terminal_states:
                return status == "COMPLETED"
            time.sleep(poll_interval)

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

    def resolve_log_path(
        self,
        pattern: str,
        job_id: str,
        array_task_id: str | int | None = None,
        job_name: str | None = None,
        replace_unknown_with_wildcard: bool = False,
    ) -> str:
        return expand_slurm_pattern(
            pattern,
            job_id=job_id,
            array_task_id=array_task_id,
            job_name=job_name,
            replace_unknown_with_wildcard=replace_unknown_with_wildcard,
        )


# --- Pure script formatting functions ---


def _expand_path(p: str) -> str:
    if p.startswith("~"):
        return "$HOME" + p[1:]
    return p


def _format_sweep_script(
    *,
    array_spec: str,
    study_name: str,
    cache_host: str,
    remote_dir: str,
    partition: str,
    time: str | None,
    mem: str,
    backend_overrides: dict[str, str],
    wrapped_setup: str,
    wrapped_trial: str,
    output_dir: str,
) -> str:
    cache_host = _expand_path(cache_host)
    remote_dir = _expand_path(remote_dir)
    slurm_opts: dict[str, str] = {
        k: v
        for k, v in {
            "partition": partition,
            "time": time,
            "mem": mem,
            **backend_overrides,
        }.items()
        if v is not None
    }

    if "output" not in slurm_opts:
        slurm_opts["output"] = f"{cache_host}/logs/%A_%a.out"
    if "error" not in slurm_opts:
        slurm_opts["error"] = f"{cache_host}/logs/%A_%a.err"

    lines = [
        "#!/usr/bin/env bash",
        "#SBATCH --parsable",
        f"#SBATCH --array={array_spec}",
    ]
    for key, value in slurm_opts.items():
        lines.append(f"#SBATCH --{key}={value}")
    lines.append("")

    lines.append(f"mkdir -p {output_dir}")
    lines.append(f"cd {remote_dir}")
    lines.append("REMOTE_DIR=$(cd . && pwd)")
    lines.append("export JERNERICS_HPC=1")

    lines.append("")
    lines.append(f"mkdir -p {cache_host}/optuna")
    lines.append(f"flock {cache_host}/optuna/init.lock {wrapped_setup}")
    lines.append(f"mkdir -p {cache_host}/tracking/{study_name}")
    lines.append("")
    lines.append(wrapped_trial)

    return "\n".join(lines)


def _format_checker_script(
    *,
    cache_host: str,
    remote_dir: str,
    partition: str,
    wrapped_checker: str,
    dependency_job_id: str | None = None,
) -> str:
    cache_host = _expand_path(cache_host)
    remote_dir = _expand_path(remote_dir)
    lines = [
        "#!/usr/bin/env bash",
        "#SBATCH --parsable",
        f"#SBATCH --partition={partition}",
        "#SBATCH --time=0:10:00",
        "#SBATCH --mem=1G",
        f"#SBATCH --output={cache_host}/logs/checker_%j.out",
        f"#SBATCH --error={cache_host}/logs/checker_%j.err",
    ]
    if dependency_job_id is not None:
        lines.append(f"#SBATCH --dependency=afterany:{dependency_job_id}")
    lines.extend(
        [
            "",
            f"cd {remote_dir}",
            "REMOTE_DIR=$(cd . && pwd)",
            wrapped_checker,
        ]
    )
    return "\n".join(lines)


def _compose_chain(array_script: str, checker_script: str) -> str:
    """Compose array + checker scripts into a single bash invocation.

    Captures the array job ID, then submits the checker with a dependency
    on it. Prints both IDs on the last line.
    """
    return (
        "ARRAY_JOB_ID=$(sbatch --parsable <<'EOF'\n"
        f"{array_script}\n"
        "EOF\n"
        ")\n"
        "\n"
        "CHECKER_JOB_ID=$(sbatch --parsable"
        " --dependency=afterany:$ARRAY_JOB_ID <<'EOF'\n"
        f"{checker_script}\n"
        "EOF\n"
        ")\n"
        "\n"
        'echo "$ARRAY_JOB_ID $CHECKER_JOB_ID"'
    )
