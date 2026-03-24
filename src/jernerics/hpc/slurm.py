import subprocess
from dataclasses import dataclass


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
        result = self.ssh.run(f"sbatch --parsable {script_path}")
        return result.stdout.strip()

    def submit_inline(self, script_content: str) -> str:
        result = self.ssh.run("sbatch --parsable", check=False)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to submit job: {result.stderr}")

        proc = subprocess.run(
            ["ssh", self.ssh.host, "sbatch --parsable"],
            input=script_content,
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    def list_jobs(self, include_completed: bool = False) -> list[SlurmJob]:
        if include_completed:
            result = self.ssh.run(
                "squeue -u $USER -o '%i|%j|%T|%P|%M|%N' 2>/dev/null || echo ''"
            )
        else:
            result = self.ssh.run(
                "squeue -u $USER -o '%i|%j|%T|%P|%M|%N' 2>/dev/null || echo ''"
            )

        jobs = []
        lines = result.stdout.strip().split("\n")
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 6:
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
        return jobs

    def cancel(self, job_id: str) -> bool:
        result = self.ssh.run(f"scancel {job_id}", check=False)
        return result.returncode == 0

    def cancel_all(self) -> bool:
        result = self.ssh.run("scancel -u $USER", check=False)
        return result.returncode == 0

    def get_status(self, job_id: str) -> str | None:
        result = self.ssh.run(f"squeue -j {job_id} -o '%T' -h", check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return None

    def wait_for_completion(self, job_id: str, poll_interval: int = 30) -> bool:
        import time

        while True:
            status = self.get_status(job_id)
            if status is None:
                return True
            if status in ["COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL"]:
                return status == "COMPLETED"
            time.sleep(poll_interval)

    def get_job_output_path(self, job_id: str, output_pattern: str) -> str:
        return output_pattern.replace("%j", job_id).replace("%A", job_id)
