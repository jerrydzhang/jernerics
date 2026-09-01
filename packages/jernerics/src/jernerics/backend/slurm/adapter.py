import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from jernerics.backend.adapter import SweepSubmissionParams
from jernerics.backend.models import JobInfo, JobSubmission, SubmitResult
from jernerics.backend.path_resolver import (
    strip_project_template,
    substitute_project_name,
)
from jernerics.config import BackendConfig, ExitCode, SlurmConfig

_SLURM_VALUE_PATTERN = re.compile(r"^[a-zA-Z0-9_.:/\-]+$")
_SLURM_JOB_ID_PATTERN = re.compile(r"^(\d+)(;\S+)?$")

SBATCH_OVERRIDE_KEYS = frozenset(
    {
        "partition",
        "time",
        "mem",
        "account",
        "cpus-per-task",
        "gpus",
        "gres",
        "constraint",
        "exclude",
        "nodes",
        "ntasks",
        "output",
        "error",
    }
)


SLURM_TERMINAL_STATES = frozenset(
    {
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
)


def unknown_sbatch_override_message(unknown: set[str]) -> str:
    keys = ", ".join(sorted(unknown))
    valid = ", ".join(sorted(SBATCH_OVERRIDE_KEYS))
    return f"Unknown sbatch override key(s): {keys}. Valid keys: {valid}"


class SlurmSubmitError(RuntimeError):
    pass


def _validate_slurm_value(value: str, name: str) -> str:
    if not _SLURM_VALUE_PATTERN.match(value):
        raise ValueError(
            f"Invalid {name} value '{value}': contains disallowed characters. "
            "Only alphanumeric, underscore, hyphen, period, colon, and slash allowed."
        )
    return value


def _validate_job_id(job_id: str, *, stderr: str) -> str:
    if not _SLURM_JOB_ID_PATTERN.match(job_id):
        raise SlurmSubmitError(f"sbatch returned invalid job id {job_id!r}: {stderr}")
    return job_id


def _expand_path(p: str) -> str:
    if p.startswith("~"):
        return "$HOME" + p[1:]
    return p


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


def _find_job_meta(meta: dict | None, job_id: str) -> Path | None:
    """Locate saved job meta for a job id, falling back to its array base id.

    Array elements arrive as "<base>_<index>" but submit saves one meta file
    per array job under the base id.
    """
    local_cache_dir = (meta or {}).get("local_cache_dir")
    if local_cache_dir is None:
        return None
    candidates = [local_cache_dir / "jobs" / f"{job_id}.json"]
    if "_" in job_id:
        candidates.append(local_cache_dir / "jobs" / f"{job_id.split('_')[0]}.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _pump_tail_output(proc: subprocess.Popen[str]) -> None:
    """Print the tail process's stdout lines as they arrive until EOF."""
    stream = proc.stdout
    if stream is None:
        return
    for line in iter(stream.readline, ""):
        sys.stdout.write(line)
        sys.stdout.flush()


def _stop_tail(proc: subprocess.Popen[str], pump: threading.Thread) -> None:
    """Terminate the tail process, drain its remaining output, and reap it."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    pump.join(timeout=2.0)
    sys.stdout.flush()


def _resolve_output_dir(output_path: str) -> str:
    if "%" in output_path:
        before_pattern = output_path[: output_path.index("%")]
        last_slash = before_pattern.rfind("/")
        return before_pattern[:last_slash] if last_slash >= 0 else "."
    return str(os.path.dirname(output_path))


def _format_array_script(
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

    output_dir = _resolve_output_dir(slurm_opts["output"])

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
    time: str,
    mem: str,
    wrapped_checker: str,
    dependency_job_id: str | None = None,
) -> str:
    cache_host = _expand_path(cache_host)
    remote_dir = _expand_path(remote_dir)
    lines = [
        "#!/usr/bin/env bash",
        "#SBATCH --parsable",
        f"#SBATCH --partition={partition}",
        f"#SBATCH --time={time}",
        f"#SBATCH --mem={mem}",
        f"#SBATCH --output={cache_host}/logs/checker_%j.out",
        f"#SBATCH --error={cache_host}/logs/checker_%j.err",
        "#SBATCH --kill-on-invalid-dep=yes",
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
    return (
        "ARRAY_JOB_ID=$(sbatch --parsable <<'EOF'\n"
        f"{array_script}\n"
        "EOF\n"
        ') || { echo "array job submission failed" >&2; exit 1; }\n'
        '[ -n "$ARRAY_JOB_ID" ] || '
        '{ echo "array job submission failed (empty job id)" >&2; exit 1; }\n'
        "\n"
        "CHECKER_JOB_ID=$(sbatch --parsable"
        " --dependency=afterany:$ARRAY_JOB_ID <<'EOF'\n"
        f"{checker_script}\n"
        "EOF\n"
        ') || { echo "checker submission failed; '
        'array job $ARRAY_JOB_ID already queued" >&2; exit 1; }\n'
        '[ -n "$CHECKER_JOB_ID" ] || '
        '{ echo "checker submission failed (empty job id); '
        'array job $ARRAY_JOB_ID already queued" >&2; exit 1; }\n'
        "\n"
        'echo "$ARRAY_JOB_ID $CHECKER_JOB_ID"'
    )


class SlurmAdapter:
    def __init__(
        self,
        host,
        *,
        remote_dir: str,
        partition: str = "priority",
        time: str | None = "1:00:00",
        mem: str = "16G",
        cpus: int = 4,
        max_concurrent_jobs: int = 10,
        exclude: str | None = None,
        cache_host: str = "",
        post_hook_partition: str | None = None,
        post_hook_time: str = "0:10:00",
        post_hook_mem: str = "1G",
    ):
        self.host = host
        self.remote_dir = remote_dir
        self.partition = partition
        self.time = time
        self.mem = mem
        self.cpus = cpus
        self.max_concurrent_jobs = max_concurrent_jobs
        self.exclude = exclude
        self.cache_host = cache_host
        self.post_hook_partition = post_hook_partition
        self.post_hook_time = post_hook_time
        self.post_hook_mem = post_hook_mem

    @classmethod
    def from_config(
        cls,
        backend_config: BackendConfig,
        *,
        host,
        project_name: str = "",
    ) -> "SlurmAdapter":
        assert host is not None
        assert isinstance(backend_config.backend, SlurmConfig)
        slurm = backend_config.backend

        shared = backend_config.shared
        remote_dir = substitute_project_name(
            shared.remote_dir.replace("~", host.home), project_name
        )
        cache_dir = (
            shared.cache_dir.replace("~", host.home) if shared.cache_dir else None
        )
        # Resolve cache_host the same way PathResolver does
        # (project_name will be set at sweep time via params, so use base path)
        if cache_dir:
            cache_host = strip_project_template(cache_dir)
        else:
            cache_host = f"{host.home}/.cache/jernerics"

        return cls(
            host=host,
            remote_dir=remote_dir,
            partition=slurm.partition,
            time=slurm.time,
            mem=slurm.mem,
            cpus=slurm.cpus,
            max_concurrent_jobs=slurm.max_concurrent_jobs,
            exclude=slurm.exclude,
            cache_host=cache_host,
            post_hook_partition=slurm.post_hook_partition,
            post_hook_time=slurm.post_hook_time,
            post_hook_mem=slurm.post_hook_mem,
        )

    def _array_spec(self, params: SweepSubmissionParams) -> str:
        max_parallel = params.max_parallel or self.max_concurrent_jobs
        if max_parallel > 0:
            return f"1-{params.n_trials}%{max_parallel}"
        return f"1-{params.n_trials}"

    def _render_overrides(self, params: SweepSubmissionParams) -> dict[str, str]:
        slurm_opts = {**params.overrides}
        unknown = set(slurm_opts) - SBATCH_OVERRIDE_KEYS
        if unknown:
            raise ValueError(unknown_sbatch_override_message(unknown))
        # Pop output/error so we can expand ~ in them
        output_pattern = slurm_opts.pop("output", None)
        error_pattern = slurm_opts.pop("error", None)
        if output_pattern:
            slurm_opts["output"] = _expand_path(output_pattern)
        if error_pattern:
            slurm_opts["error"] = _expand_path(error_pattern)
        return slurm_opts

    def render_sweep(self, params: SweepSubmissionParams) -> str:
        cache_host = params.cache_dir or self.cache_host
        array_script = self._render_array_script(params, cache_host=cache_host)

        if params.post_hook_command is None:
            return array_script

        checker_script = _format_checker_script(
            cache_host=cache_host,
            remote_dir=self.remote_dir,
            partition=self.post_hook_partition
            or params.overrides.get("partition", self.partition),
            time=self.post_hook_time,
            mem=self.post_hook_mem,
            wrapped_checker=params.post_hook_command,
        )
        return _compose_chain(array_script, checker_script)

    def _render_array_script(
        self, params: SweepSubmissionParams, *, cache_host: str
    ) -> str:
        return _format_array_script(
            array_spec=self._array_spec(params),
            study_name=params.study_name,
            cache_host=cache_host,
            remote_dir=self.remote_dir,
            partition=self.partition,
            time=self.time,
            mem=self.mem,
            backend_overrides=self._render_overrides(params),
            wrapped_setup=params.setup_command,
            wrapped_trial=params.trial_command,
        )

    def submit_sweep(self, params: SweepSubmissionParams) -> SubmitResult:
        script = self.render_sweep(params)

        if params.post_hook_command is None:
            result = self.host.run(
                [f"cd {self.remote_dir} && sbatch --parsable"],
                input=script,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise SlurmSubmitError(f"Failed to submit job: {result.stderr.strip()}")
            return SubmitResult(
                submissions=[
                    JobSubmission(
                        job_id=result.stdout.strip(), n_trials=params.n_trials
                    )
                ]
            )

        result = self.host.run(
            ["bash"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SlurmSubmitError(
                f"Failed to submit job chain: {result.stderr.strip()}"
            )

        parts = result.stdout.strip().split(" ", 1)
        job_id = _validate_job_id(parts[0], stderr=result.stderr.strip())
        subs = [JobSubmission(job_id=job_id, n_trials=params.n_trials)]
        if len(parts) > 1:
            checker_id = _validate_job_id(parts[1], stderr=result.stderr.strip())
            subs.append(JobSubmission(job_id=checker_id, n_trials=0, role="checker"))
        return SubmitResult(submissions=subs)

    def submit_job(
        self, script: str, *, name: str = "build", log_dir: str | None = None
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
        if self.exclude:
            parts = [p.strip() for p in self.exclude.split(",") if p.strip()]
            if parts:
                nodes = ",".join(_validate_slurm_value(p, "exclude") for p in parts)
                sbatch_script += f"#SBATCH --exclude={nodes}\n"
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
            ') || { echo "build job submission failed" >&2; exit 1; }',
            '[ -n "$BUILD_JOB_ID" ] || '
            '{ echo "build job submission failed (empty job id)" >&2; exit 1; }',
            "echo $BUILD_JOB_ID",
        ]
        submission_script = "\n".join(lines)

        result = self.host.run(
            ["bash"],
            input=submission_script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SlurmSubmitError(
                f"Failed to submit build job: {result.stderr.strip()}"
            )
        return _validate_job_id(result.stdout.strip(), stderr=result.stderr.strip())

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

    def _dependent_job_ids(self, job_id: str) -> list[str]:
        # A sweep submits an array job plus a checker with
        # --dependency=afterany:$ARRAY_JOB_ID. scancel on the array job
        # satisfies afterany, leaving the checker PENDING forever, so cancel
        # must also kill every job depending on the target. %E reports each
        # queued job's dependency expression.
        result = self.host.run(
            ["squeue -u $USER -h -o '%i|%E' 2>/dev/null"],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        pattern = re.compile(rf"(?<![0-9]){re.escape(job_id)}(?:_[0-9]+)?(?![0-9])")
        dependents: list[str] = []
        for line in result.stdout.strip().split("\n"):
            if "|" not in line:
                continue
            dep_id, dep_expr = line.split("|", 1)
            if pattern.search(dep_expr):
                dependents.append(dep_id.strip())
        return dependents

    def cancel(self, job_id: str) -> bool:
        targets = [job_id, *self._dependent_job_ids(job_id)]
        result = self.host.run(["scancel", *targets], check=False)
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
        while True:
            if timeout is not None and (time.time() - start_time) >= timeout:
                raise TimeoutError(
                    f"Timeout waiting for job {job_id} after {timeout} seconds"
                )
            status = self.get_status(job_id)
            if status is None:
                return True
            if status in SLURM_TERMINAL_STATES:
                return status == "COMPLETED"
            time.sleep(poll_interval)

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        meta: dict | None = None,
    ) -> None:
        meta_file = _find_job_meta(meta, job_id)
        if meta_file is not None:
            meta_data = json.loads(meta_file.read_text())
            output_pattern = meta_data.get("output_pattern", "logs/slurm_%j.out")
            error_pattern = meta_data.get("error_pattern", "logs/slurm_%j.err")
            meta_remote_dir = meta_data.get("remote_dir", self.remote_dir)
            n_trials = meta_data.get("n_trials", 1)
        else:
            output_pattern = None
            error_pattern = None
            meta_remote_dir = self.remote_dir
            n_trials = 1

        if output_pattern is None or error_pattern is None:
            cache_host = (meta or {}).get("cache_host") or self.cache_host
            output_pattern = f"{cache_host}/logs/%A_%a.out"
            error_pattern = f"{cache_host}/logs/%A_%a.err"

        log_pattern = error_pattern if stderr else output_pattern

        base_job_id = job_id.split("_")[0] if "_" in job_id else job_id
        array_idx = job_id.split("_")[1] if "_" in job_id else None

        effective_array_index = array_idx
        if effective_array_index is None and n_trials == 1:
            effective_array_index = 1

        log_file = expand_slurm_pattern(
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

            resolved = self._resolve_status_job(base_job_id, effective_array_index)
            if resolved is None or resolved[1] in SLURM_TERMINAL_STATES:
                self._cat_log(log_file, "Log file not found")
                return
            status_job_id, _ = resolved

            tail_proc = subprocess.Popen(
                ["ssh", self.host.host, "tail", "-n", "+1", "-f", log_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            pump = threading.Thread(
                target=_pump_tail_output, args=(tail_proc,), daemon=True
            )
            pump.start()
            try:
                self.wait_for_completion(status_job_id, poll_interval=10)
            except KeyboardInterrupt:
                pass
            finally:
                _stop_tail(tail_proc, pump)
                final_state = self.get_status(status_job_id) or "UNKNOWN"
                print(f"--- job {status_job_id} {final_state}: follow ended ---")
        else:
            self._cat_log(log_file, "Log file not found")

    def _resolve_status_job(
        self, base_job_id: str, array_index: str | int | None
    ) -> tuple[str, str] | None:
        """Return the first candidate job id that resolves, with its state.

        Plain jobs are polled by their base id; array elements may only be
        visible as "<base>_<index>".
        """
        candidates = [base_job_id]
        if array_index is not None:
            candidates.append(f"{base_job_id}_{array_index}")
        for candidate in candidates:
            status = self.get_status(candidate)
            if status is not None:
                return candidate, status
        return None

    def _cat_log(self, log_file: str, missing: str) -> None:
        """Print a log file, retrying briefly while it appears."""
        max_retries = 5
        retry_delay = 1.0
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
        print(f"Error: {missing}: {log_file}")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    def cleanup(self) -> None:
        pass
