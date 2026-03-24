import subprocess
from pathlib import Path

from jernerics._cli_helpers import (
    find_pyproject_dir,
    load_jernerics_config,
)
from jernerics.container.templates import generate_container_def
from jernerics.hpc.slurm import SlurmJobManager
from jernerics.hpc.ssh import SSHClient
from jernerics.hpc.sync import FileSyncer


class ContainerBuilder:
    def __init__(self, project_dir: str | Path | None = None):
        if project_dir is None:
            project_dir = find_pyproject_dir()
            if project_dir is None:
                raise ValueError(
                    "No pyproject.toml found in current directory or parents"
                )

        self.project_dir = Path(project_dir)
        self.config, _ = load_jernerics_config(self.project_dir)

        if not self.config.host:
            raise ValueError(
                "HPC host not configured. Set JERNERICS_HPC_HOST environment variable "
                "or [tool.jernerics.hpc].host in pyproject.toml"
            )

        self.ssh = SSHClient(self.config.host)
        self.syncer = FileSyncer(self.ssh, self._get_remote_dir())
        self.slurm = SlurmJobManager(self.ssh)

    def _get_remote_dir(self) -> str:
        project_name = self.project_dir.resolve().name
        remote_dir = self.config.remote_dir.replace("{project_name}", project_name)
        return remote_dir.rstrip("/")

    def _generate_build_script(self) -> str:
        return f"""#!/bin/bash
#SBATCH --job-name=container-build
#SBATCH --partition={self.config.partition}
#SBATCH --time={self.config.time}
#SBATCH --mem={self.config.mem}
#SBATCH --cpus-per-task={self.config.cpus}
#SBATCH --output=build_%j.out
#SBATCH --error=build_%j.err

set -e

echo "=== Build started at $(date) ==="
echo "Running on $(hostname)"

cd {self._get_remote_dir()}

echo
echo "--- Building container with Apptainer + uv sync ---"
time apptainer build --fakeroot --force container.sif container.def

echo
echo "--- Build result ---"
ls -lh container.sif

echo
echo "=== Build completed at $(date) ==="
"""

    def needs_rebuild(self, force: bool = False) -> bool:
        if force:
            return True

        lock_path = self.project_dir / "uv.lock"
        if not lock_path.exists():
            raise FileNotFoundError("uv.lock not found. Run 'uv lock' first.")

        return self.syncer.container_needs_rebuild(lock_path)

    def ensure_container_def(self) -> bool:
        local_def = self.project_dir / "container.def"
        if local_def.exists():
            return False

        content = generate_container_def("python")
        local_def.write_text(content)
        return True

    def build(self, force: bool = False, dry_run: bool = False) -> str | None:
        lock_path = self.project_dir / "uv.lock"
        if not lock_path.exists():
            raise FileNotFoundError("uv.lock not found. Run 'uv lock' first.")

        if not dry_run and not self.needs_rebuild(force):
            print("Container is up to date. Use --force to rebuild.")
            return None

        self.ensure_container_def()

        if dry_run:
            print("=== DRY RUN ===")
            print(f"Project dir: {self.project_dir}")
            print(f"Remote dir: {self._get_remote_dir()}")
            print(f"HPC host: {self.config.host}")
            print()
            print("Would sync files and submit build job with:")
            print(self._generate_build_script())
            return None

        remote_dir = self._get_remote_dir()

        print(f"[1/3] Syncing project to {self.config.host}:{remote_dir}")
        self.syncer.sync_project(self.project_dir)

        build_script = self._generate_build_script()
        remote_script_path = f"{remote_dir}/build_container.sh"

        print("[2/3] Uploading build script...")
        subprocess.run(
            ["ssh", self.config.host, f"cat > {remote_script_path}"],
            input=build_script,
            text=True,
            check=True,
            capture_output=True,
        )  # type: ignore[call-overload]

        print("[3/3] Submitting build job to SLURM...")
        job_id = self.slurm.submit(remote_script_path)
        print(f"\nBuild job submitted: {job_id}")
        print("\nMonitor progress:")
        print(f"  ssh {self.config.host} 'tail -f {remote_dir}/build_{job_id}.out'")

        return job_id
