import os
import re
import time

from jernerics.backend.models import JobInfo, SubmitResult, SweepSpec
from jernerics.config import BackendConfig
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

    capabilities = frozenset()

    @classmethod
    def from_config(
        cls,
        backend_config: BackendConfig,
        *,
        host=None,
        syncer=None,
    ) -> "SlurmBackend":
        """Construct from config. Uses StdoutHost if no host provided.

        With StdoutHost, submit_sweep prints the composed bash script to
        stdout — used by the retry checker running inside a container.
        """
        from jernerics.backend.components.container import (
            Apptainer,
            Docker,
            NoContainer,
        )
        from jernerics.backend.components.host import StdoutHost

        if host is None:
            host = StdoutHost()

        container_type = backend_config.container_type
        if container_type == "apptainer":
            container = Apptainer(host)
        elif container_type == "docker":
            container = Docker(host)
        elif container_type == "none":
            container = NoContainer()
        else:
            container = Apptainer(host)

        return cls(
            host=host,
            container=container,
            syncer=syncer,
            remote_dir=backend_config.remote_dir.replace("~", "$HOME"),
            partition=backend_config.partition,
            time=backend_config.time,
            mem=backend_config.mem,
            cpus=backend_config.cpus,
            max_concurrent_jobs=backend_config.max_concurrent_jobs,
            cache_dir=backend_config.cache_dir,
            tracking_server=None,
            heartbeat_interval_s=backend_config.heartbeat_interval_s,
        )

    def _cache_host(self, project_name: str) -> str:
        if self.cache_dir:
            cache = self.cache_dir.replace("{project_name}", project_name)
            cache = cache.replace("{project-name}", project_name)
            return cache.replace("~", "$HOME")
        return f"{self.remote_dir}/.jernerics".replace("~", "$HOME")

    def _bind_args(self, cache_host: str) -> list[str]:
        return [
            '"${REMOTE_DIR}:/work"',
            f'"{cache_host}:/cache"',
        ]

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

    def _build_setup_command(
        self,
        study_name: str,
        storage_path: str,
        direction: str,
    ) -> str:
        return (
            f'python -c "'
            f"from optuna.storages.journal import JournalFileBackend, JournalStorage; "
            f"import optuna; "
            f"optuna.create_study("
            f"study_name={study_name!r},"
            f" storage=JournalStorage(JournalFileBackend({storage_path!r})),"
            f" direction={direction!r},"
            f' load_if_exists=True)"'
        )

    def _build_trial_command(
        self,
        dag_relpath: str,
        config_relpath: str,
        study_name: str,
        storage_path: str,
        project_name: str | None,
        tracking_dir: str,
        tracking_server: str | None,
        heartbeat_interval_s: float = -1.0,
    ) -> str:
        args = [
            "python",
            "-m",
            "jernerics.runner",
            f"/work/{dag_relpath}",
            f"/work/{config_relpath}",
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
        if heartbeat_interval_s > 0:
            args.extend(["--heartbeat-interval", str(heartbeat_interval_s)])
        return " \\\n        ".join(args)

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

    def _build_array_script(
        self,
        spec: SweepSpec,
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
        tracking_dir = "/cache/tracking/" + spec.study_name

        setup_command = self._build_setup_command(
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            direction=direction,
        )
        trial_command = self._build_trial_command(
            dag_relpath=dag_relpath,
            config_relpath=config_relpath,
            study_name=spec.study_name,
            storage_path=spec.storage_url,
            project_name=spec.project_name,
            tracking_dir=tracking_dir,
            tracking_server=self.tracking_server,
            heartbeat_interval_s=self.heartbeat_interval_s,
        )

        return self._generate_sweep_script(
            setup_command=setup_command,
            trial_command=trial_command,
            array_spec=array_spec,
            study_name=spec.study_name,
            project_name=spec.project_name or "",
            slurm_overrides=spec.slurm_overrides,
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
        checker_cmd = self._build_checker_command(ctx_path, chain_depth)
        wrapped_checker = self.container.wrap(
            f"{checker_cmd} 2>/dev/null", self._bind_args(cache_host)
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
        spec: SweepSpec,
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
        cache_host = self._cache_host(project_name)
        partition = spec.slurm_overrides.get("partition", self.partition)

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

    def _generate_sweep_script(
        self,
        setup_command: str,
        trial_command: str,
        array_spec: str,
        study_name: str,
        project_name: str,
        slurm_overrides: dict[str, str],
    ) -> str:
        cache_host = self._cache_host(project_name)
        bind_args = self._bind_args(cache_host)
        wrapped_setup = self.container.wrap(setup_command, bind_args)
        wrapped_trial = self.container.wrap(trial_command, bind_args)

        slurm_opts = {**slurm_overrides}
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
            slurm_overrides=slurm_opts,
            wrapped_setup=wrapped_setup,
            wrapped_trial=wrapped_trial,
            output_dir=output_dir,
        )

    def _cache_path(self) -> str:
        if self.cache_dir:
            return self.cache_dir.replace("~", "$HOME")
        return f"{self.remote_dir}/.jernerics".replace("~", "$HOME")

    def submit_build_job(self) -> str:
        partition = _validate_slurm_value(self.partition, "partition")
        time_val = _validate_slurm_value(self.time or "1:00:00", "time")
        mem = _validate_slurm_value(self.mem, "mem")
        cpus = _validate_slurm_value(str(self.cpus), "cpus")
        output_dir = f"{self._cache_path()}/logs"

        script = f"""#!/bin/bash
#SBATCH --job-name=container-build
#SBATCH --partition={partition}
#SBATCH --time={time_val}
#SBATCH --mem={mem}
#SBATCH --cpus-per-task={cpus}
#SBATCH --output={output_dir}/build_%j.out
#SBATCH --error={output_dir}/build_%j.err

set -e

echo "=== Build started at $(date) ==="
echo "Running on $(hostname)"

export APPTAINER_TMPDIR=/dev/shm/apptainer-build-$SLURM_JOB_ID
mkdir -p $APPTAINER_TMPDIR
trap 'rm -rf $APPTAINER_TMPDIR' EXIT

cd {self.remote_dir}

echo
echo "--- Building container with Apptainer ---"
time apptainer build --fakeroot --force container.sif container.def

echo
echo "--- Build result ---"
ls -lh container.sif

echo
echo "=== Build completed at $(date) ==="
"""

        result = self.host.run(
            ["sbatch", "--parsable"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit build job: {result.stderr.strip()}")
        return result.stdout.strip()

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
    slurm_overrides: dict[str, str],
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
            **slurm_overrides,
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
