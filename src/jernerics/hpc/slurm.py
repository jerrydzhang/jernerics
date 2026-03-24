import os
import shlex
from dataclasses import dataclass


def expand_slurm_pattern(
    pattern: str,
    job_id: str | None = None,
    array_task_id: str | int | None = None,
    job_name: str | None = None,
    replace_unknown_with_wildcard: bool = False,
) -> str:
    """Expand SLURM filename patterns.

    Supported patterns:
    - %j: Job ID
    - %A: Array job's master job ID
    - %a: Array task ID
    - %x: Job name
    - %u: Username
    - %N: Node name

    Args:
        pattern: The pattern string with SLURM placeholders
        job_id: Job ID to substitute for %j and %A
        array_task_id: Array task ID to substitute for %a
        job_name: Job name to substitute for %x
        replace_unknown_with_wildcard: If True, replace unknown patterns with '*'
    """
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


@dataclass
class SlurmJob:
    job_id: str
    name: str
    status: str
    partition: str
    time: str
    nodes: str


class SlurmJobManager:
    def __init__(self, ssh_client):
        self.ssh = ssh_client

    def submit(self, script_path: str) -> str:
        result = self.ssh.run(
            f"sbatch --parsable {shlex.quote(script_path)}", check=False
        )
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit job: {result.stderr.strip()}")
        return result.stdout.strip()

    def submit_inline(self, script_content: str, workdir: str | None = None) -> str:
        if workdir:
            cmd = f"cd {shlex.quote(workdir)} && sbatch --parsable"
        else:
            cmd = "sbatch --parsable"

        result = self.ssh.run(cmd, check=False, input=script_content)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit job: {result.stderr.strip()}")

        return result.stdout.strip()

    def list_jobs(self, include_completed: bool = False) -> list[SlurmJob]:
        fmt = "%i\\t%j\\t%T\\t%P\\t%M\\t%N"
        result = self.ssh.run(f"squeue -u $USER -o '{fmt}' 2>/dev/null || echo ''")

        jobs: list[SlurmJob] = []
        seen_ids: set[str] = set()
        lines = result.stdout.strip().split("\n")
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) >= 6:
                seen_ids.add(parts[0])
                jobs.append(
                    SlurmJob(
                        job_id=parts[0],
                        name=parts[1],
                        status=parts[2],
                        partition=parts[3],
                        time=parts[4],
                        nodes=parts[5],
                    )
                )

        if include_completed:
            sacct_fmt = (
                "JobID%20,JobName%50,State%15,Partition%15,Elapsed%12,AllocNodes%15"
            )
            sacct_result = self.ssh.run(
                f"sacct -u $USER --starttime $(date -d '1 day ago' +%Y-%m-%d) "
                f"--format={sacct_fmt} --noheader --parsable2 2>/dev/null || echo ''"
            )
            for line in sacct_result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("|")
                if len(parts) >= 6:
                    job_id = parts[0].strip()
                    if job_id not in seen_ids:
                        seen_ids.add(job_id)
                        jobs.append(
                            SlurmJob(
                                job_id=job_id,
                                name=parts[1].strip(),
                                status=parts[2].strip(),
                                partition=parts[3].strip(),
                                time=parts[4].strip(),
                                nodes=parts[5].strip(),
                            )
                        )

        return jobs

    def cancel(self, job_id: str) -> bool:
        result = self.ssh.run(f"scancel {shlex.quote(job_id)}", check=False)
        return result.returncode == 0

    def cancel_all(self) -> bool:
        result = self.ssh.run("scancel -u $USER", check=False)
        return result.returncode == 0

    def get_status(self, job_id: str) -> str | None:
        result = self.ssh.run(
            f"squeue -j {shlex.quote(job_id)} -o '%T' -h", check=False
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        sacct_result = self.ssh.run(
            f"sacct -j {shlex.quote(job_id)} --format=State --noheader --parsable2",
            check=False,
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
        self, job_id: str, poll_interval: int = 30, timeout: int | None = None
    ) -> bool:
        import time

        start_time = time.time()
        while True:
            if timeout is not None:
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    raise TimeoutError(
                        f"Timeout waiting for job {job_id} after {timeout} seconds"
                    )
            status = self.get_status(job_id)
            if status is None:
                return True
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
            if status in terminal_states:
                return status == "COMPLETED"
            time.sleep(poll_interval)

    def get_job_output_path(
        self,
        job_id: str,
        output_pattern: str,
        job_name: str | None = None,
        array_task_id: str | int | None = None,
        replace_unknown_with_wildcard: bool = False,
    ) -> str:
        return expand_slurm_pattern(
            output_pattern,
            job_id=job_id,
            job_name=job_name,
            array_task_id=array_task_id,
            replace_unknown_with_wildcard=replace_unknown_with_wildcard,
        )
