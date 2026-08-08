"""Interactive GPU allocation + container shell.

The interactive path is deliberately separate from batch sweep submission:

- An ``sbatch`` reservation job (``sleep infinity``) holds the allocation and
  survives SSH disconnect — unlike ``srun --pty``, which dies when the SSH
  session drops and loses the allocation.
- The user reaches the compute node over ``ssh -o ProxyJump=<login>``. The
  cluster gates node SSH via ``pam_slurm_adopt``, so an active job is required.
- The SSH command runs ``apptainer shell`` directly, dropping the user inside
  the container in ``/work``. Process persistence (tmux, screen, etc.) is left
  to the user — jernerics owns the allocation and container entry, not the
  shell environment.

This module owns allocation logic and SSH command construction only — it reuses
``SSHHost`` for every remote call.
"""

import subprocess
import time
from dataclasses import dataclass

from jernerics.backend.slurm.adapter import _validate_slurm_value

_TERMINAL_STATES = frozenset(
    {
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "BOOT_FAIL",
        "COMPLETED",
    }
)


@dataclass
class InteractiveSessionInfo:
    """One matching job line from ``squeue``."""

    job_id: str
    state: str
    node: str | None


def extract_node(node_list: str) -> str | None:
    """Return the first compute-node hostname from a SLURM ``%N`` field.

    A single-node interactive allocation yields a bare hostname (``gpu13``);
    empty/``None``/``n/a`` means the job has not been assigned a node yet.
    """
    node_list = node_list.strip()
    if not node_list or node_list.lower() in ("none", "n/a"):
        return None
    return node_list.split(",")[0].strip()


def parse_session_lines(output: str) -> list[InteractiveSessionInfo]:
    """Parse ``squeue -o '%i|%T|%N' -h`` output into session infos.

    Skips a header row if present (lines containing ``JOBID`` or ``NODELIST``).
    """
    sessions: list[InteractiveSessionInfo] = []
    for line in output.strip().splitlines():
        line = line.strip()
        if not line or "JOBID" in line or "NODELIST" in line:
            continue
        parts = line.split("|")
        if len(parts) < 2:
            continue
        job_id = parts[0].strip()
        state = parts[1].strip()
        node = extract_node(parts[2]) if len(parts) > 2 else None
        sessions.append(InteractiveSessionInfo(job_id=job_id, state=state, node=node))
    return sessions


def format_interactive_script(
    *,
    job_name: str,
    partition: str,
    time_limit: str,
    mem: str,
    cpus: int,
    gpus: int,
    constraint: str | None = None,
) -> str:
    """Render the ``sleep infinity`` sbatch reservation script."""
    for value, label in (
        (job_name, "job-name"),
        (partition, "partition"),
        (time_limit, "time"),
        (mem, "mem"),
        (constraint, "constraint"),
    ):
        if value is not None:
            _validate_slurm_value(value, label)

    lines = [
        "#!/bin/bash",
        "#SBATCH --parsable",
        f"#SBATCH --job-name={job_name}",
        f"#SBATCH --partition={partition}",
        f"#SBATCH --time={time_limit}",
        f"#SBATCH --mem={mem}",
        f"#SBATCH --cpus-per-task={cpus}",
        f"#SBATCH --gres=gpu:{gpus}",
    ]
    if constraint:
        lines.append(f"#SBATCH --constraint={constraint}")
    lines.append("")
    lines.append("sleep infinity")
    return "\n".join(lines)


class InteractiveSession:
    """Allocate a GPU node and attach a container shell to it."""

    def __init__(
        self,
        host,
        *,
        job_name: str,
        remote_dir: str,
        container_image: str,
        cache_host: str,
        partition: str,
        time_limit: str,
        gpus: int,
        mem: str,
        cpus: int,
        constraint: str | None = None,
        login_target: str | None = None,
        user: str | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.host = host
        self.job_name = job_name
        self.remote_dir = remote_dir
        self.container_image = container_image
        self.cache_host = cache_host
        self.partition = partition
        self.time_limit = time_limit
        self.gpus = gpus
        self.mem = mem
        self.cpus = cpus
        self.constraint = constraint
        self.login_target = login_target or getattr(host, "host", None)
        self.user = user
        self.poll_interval = poll_interval

    # ── allocation ──────────────────────────────────────────────────────────

    def find_existing(self) -> InteractiveSessionInfo | None:
        """Return the running/pending interactive job for this name, if any."""
        result = self.host.run(
            [
                f"squeue --name={self.job_name} --me"
                f" -o '%i|%T|%N' -h 2>/dev/null || true"
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        sessions = parse_session_lines(result.stdout)
        return sessions[0] if sessions else None

    def submit(self) -> str:
        """Submit the reservation job; return its job id."""
        script = format_interactive_script(
            job_name=self.job_name,
            partition=self.partition,
            time_limit=self.time_limit,
            mem=self.mem,
            cpus=self.cpus,
            gpus=self.gpus,
            constraint=self.constraint,
        )
        result = self.host.run(
            ["sbatch --parsable"],
            input=script,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to submit interactive job: {result.stderr.strip()}"
            )
        stdout = result.stdout.strip()
        if not stdout:
            raise RuntimeError("sbatch returned no job id")
        return stdout.splitlines()[0].strip()

    def wait_for_running(self, job_id: str, *, timeout: float | None = None) -> str:
        """Poll until the job is RUNNING; return its node hostname."""
        start = time.time()
        while True:
            result = self.host.run(
                [f"squeue -j {job_id} -o '%T|%N' -h 2>/dev/null || true"],
                check=False,
                capture_output=True,
                text=True,
            )
            line = result.stdout.strip()
            if line:
                state, _, node_field = line.partition("|")
                state = state.strip()
                if state == "RUNNING":
                    node = extract_node(node_field)
                    if node:
                        return node
                if state in _TERMINAL_STATES:
                    raise RuntimeError(
                        f"Interactive job {job_id} ended in state {state}"
                        " before becoming RUNNING"
                    )
            if timeout is not None and (time.time() - start) >= timeout:
                raise TimeoutError(f"Timed out waiting for job {job_id} to start")
            time.sleep(self.poll_interval)

    # ── connection ──────────────────────────────────────────────────────────

    def _node_target(self, node: str) -> str:
        if self.user:
            return f"{self.user}@{node}"
        return node

    def remote_shell_command(self, node: str) -> str:
        """Command run on the compute node: apptainer shell in the project dir.

        The user lands inside the container at ``/work`` (the bind-mounted
        project source). No tmux wrapper — the user owns their own shell
        environment and process persistence (``tmux``, ``screen``, etc.).
        """
        binds = f"{self.remote_dir}:/work --bind {self.cache_host}:/cache"
        return (
            f"cd {self.remote_dir} &&"
            f" apptainer shell --nv --pwd /work --bind {binds}"
            f" {self.container_image}"
        )

    def ssh_argv(self, node: str) -> list[str]:
        """Build the local ssh argv (ProxyJump through the login node)."""
        if not self.login_target:
            raise RuntimeError(
                "Interactive sessions require an SSH host; no login target set."
            )
        return [
            "ssh",
            "-t",
            "-o",
            "LogLevel=ERROR",
            "-o",
            f"ProxyJump={self.login_target}",
            self._node_target(node),
            self.remote_shell_command(node),
        ]

    def connect(self, node: str) -> int:
        """Attach to the container shell; returns ssh's exit code."""
        return subprocess.run(self.ssh_argv(node)).returncode

    # ── teardown ────────────────────────────────────────────────────────────

    def end(self) -> InteractiveSessionInfo | None:
        """Cancel any existing interactive allocation; return what was ended."""
        session = self.find_existing()
        if session is None:
            return None
        self.host.run(["scancel", session.job_id], check=False)
        return session
