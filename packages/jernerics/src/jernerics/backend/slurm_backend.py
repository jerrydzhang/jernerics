import os
import re
import time

from jernerics.backend.components.container import Apptainer
from jernerics.backend.components.host import SSHHost
from jernerics.backend.components.project_sync import FileSyncer
from jernerics.backend.models import JobInfo

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
        host: SSHHost,
        container: Apptainer,
        syncer: FileSyncer,
        *,
        remote_dir: str,
        partition: str = "priority",
        time: str | None = "1:00:00",
        mem: str = "16G",
        cpus: int = 4,
        max_concurrent_jobs: int = 10,
        cache_dir: str | None = None,
        tracking_server: str | None = None,
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

    capabilities = frozenset()

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

    def submit_sweep(
        self,
        setup_command: str,
        trial_command: str,
        *,
        n_trials: int,
        study_name: str,
        project_name: str,
        max_parallel: int | None = None,
        slurm_overrides: dict[str, str] | None = None,
    ) -> str:
        max_parallel_val = max_parallel or self.max_concurrent_jobs
        if max_parallel_val > 0:
            array_spec = f"1-{n_trials}%{max_parallel_val}"
        else:
            array_spec = f"1-{n_trials}"

        script = self._generate_sweep_script(
            setup_command=setup_command,
            trial_command=trial_command,
            array_spec=array_spec,
            study_name=study_name,
            project_name=project_name,
            slurm_overrides=slurm_overrides or {},
        )

        result = self.host.run(
            [f"cd {self.remote_dir} && sbatch --parsable"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit job: {result.stderr.strip()}")
        return result.stdout.strip()

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

        slurm_opts = {
            "partition": self.partition,
            "time": self.time,
            "mem": self.mem,
            **slurm_overrides,
        }
        slurm_opts: dict[str, str] = {
            k: v for k, v in slurm_opts.items() if v is not None
        }

        output_pattern = str(slurm_opts.get("output", f"{cache_host}/logs/%A_%a.out"))
        error_pattern = str(slurm_opts.get("error", f"{cache_host}/logs/%A_%a.err"))
        slurm_opts["output"] = self._expand_path(output_pattern)
        slurm_opts["error"] = self._expand_path(error_pattern)

        lines = [
            "#!/usr/bin/env bash",
            "#SBATCH --parsable",
            f"#SBATCH --array={array_spec}",
        ]
        for key, value in slurm_opts.items():
            lines.append(f"#SBATCH --{key}={value}")
        lines.append("")

        output_dir = self._resolve_output_dir(str(slurm_opts["output"]))
        lines.append(f"mkdir -p {output_dir}")
        lines.append(f"cd {self.remote_dir}")
        lines.append("REMOTE_DIR=$(cd . && pwd)")
        lines.append("export JERNERICS_HPC=1")

        bind_args = self._bind_args(cache_host)
        wrapped_setup = self.container.wrap(setup_command, bind_args)
        wrapped_trial = self.container.wrap(trial_command, bind_args)

        lines.append("")
        lines.append(f"mkdir -p {cache_host}/optuna")
        lines.append(f"flock {cache_host}/optuna/init.lock {wrapped_setup}")
        lines.append(f"mkdir -p {cache_host}/tracking/{study_name}")
        lines.append("")
        lines.append(wrapped_trial)

        return "\n".join(lines)

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
echo "=== Build completed at $(date) ===
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
        fmt = "%i\\t%j\\t%T"
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
            parts = line.split("\t")
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
