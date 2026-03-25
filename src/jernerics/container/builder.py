import json
import re
import subprocess
from pathlib import Path

from jernerics._cli_helpers import (
    find_pyproject_dir,
    get_project_name,
    load_jernerics_config,
)
from jernerics.container.templates import generate_container_def
from jernerics.hpc.slurm import SlurmJobManager
from jernerics.hpc.ssh import SSHClient, _quote_path
from jernerics.hpc.sync import FileSyncer

_SLURM_VALUE_PATTERN = re.compile(r"^[a-zA-Z0-9_.:/\-]+$")


def _validate_slurm_value(value: str, name: str) -> str:
    if not _SLURM_VALUE_PATTERN.match(value):
        raise ValueError(
            f"Invalid {name} value '{value}': contains disallowed characters. "
            "Only alphanumeric, underscore, hyphen, period, colon, and slash allowed."
        )
    return value


class ContainerBuilder:
    def __init__(self, project_dir: str | Path | None = None):
        if project_dir is None:
            project_dir = find_pyproject_dir()
            if project_dir is None:
                raise ValueError(
                    "No pyproject.toml found in current directory or parents"
                )

        self.project_dir = Path(project_dir)
        self.config, _, _ = load_jernerics_config(self.project_dir)
        self.project_name = get_project_name(self.project_dir)

        if not re.match(r"^[a-zA-Z0-9_.-]+$", self.project_name):
            raise ValueError(
                f"Invalid project name '{self.project_name}'. "
                "Name must contain only alphanumeric characters, "
                "underscores, hyphens, and periods."
            )

        if not self.config.host:
            raise ValueError(
                "HPC host not configured. Set JERNERICS_HPC_HOST environment variable "
                "or [tool.jernerics.hpc].host in pyproject.toml"
            )

        self.ssh = SSHClient(self.config.host)
        self.syncer = FileSyncer(self.ssh, self._get_remote_dir())
        self.slurm = SlurmJobManager(self.ssh)

    def _get_remote_dir(self) -> str:
        remote_dir = self.config.remote_dir.replace("{project_name}", self.project_name)
        return remote_dir.rstrip("/")

    def _get_cache_dir(self) -> str | None:
        if not self.config.cache_dir:
            return None
        return self.config.cache_dir.rstrip("/")

    def _get_build_tmpdir(self) -> str | None:
        cache_dir = self._get_cache_dir()
        if not cache_dir:
            return None
        return f"{cache_dir}/{self.project_name}/tmp"

    def _generate_build_script(self, slurm_output_dir: str) -> str:
        remote_dir = self._get_remote_dir()
        quoted_remote_dir = _quote_path(remote_dir)
        partition = _validate_slurm_value(self.config.partition, "partition")
        time = _validate_slurm_value(self.config.time, "time")
        mem = _validate_slurm_value(self.config.mem, "mem")
        cpus = _validate_slurm_value(str(self.config.cpus), "cpus")

        tmpdir_export = ""
        build_tmpdir = self._get_build_tmpdir()
        if build_tmpdir:
            tmpdir = _validate_slurm_value(build_tmpdir, "build_tmpdir")
            tmpdir_export = f"export APPTAINER_TMPDIR={tmpdir}\n"

        return f"""#!/bin/bash
#SBATCH --job-name=container-build
#SBATCH --partition={partition}
#SBATCH --time={time}
#SBATCH --mem={mem}
#SBATCH --cpus-per-task={cpus}
#SBATCH --output={slurm_output_dir}/build_%j.out
#SBATCH --error={slurm_output_dir}/build_%j.err

set -e

echo "=== Build started at $(date) ==="
echo "Running on $(hostname)"

{tmpdir_export}cd {quoted_remote_dir}

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

        remote_dir = self._get_remote_dir()
        slurm_output_dir = f"{self.ssh.expand_tilde(remote_dir)}/logs"

        if dry_run:
            print("=== DRY RUN ===")
            print(f"Project dir: {self.project_dir}")
            print(f"Remote dir: {remote_dir}")
            print(f"HPC host: {self.config.host}")
            build_tmpdir = self._get_build_tmpdir()
            if build_tmpdir:
                print(f"Build tmpdir: {build_tmpdir}")
            print()
            print("Would sync files and submit build job with:")
            print(self._generate_build_script(slurm_output_dir))
            return None

        print(f"[1/4] Syncing project to {self.config.host}:{remote_dir}")
        self.syncer.sync_project(self.project_dir)

        print("[2/4] Creating logs directory...")
        self.ssh.mkdir(f"{remote_dir}/logs")

        build_tmpdir = self._get_build_tmpdir()
        if build_tmpdir:
            print(f"[3/5] Creating build tmpdir ({build_tmpdir})...")
            self.ssh.mkdir(build_tmpdir)
        else:
            print("[3/5] (No cache_dir configured, using default /tmp)")

        build_script = self._generate_build_script(slurm_output_dir)
        remote_script_path = f"{remote_dir}/build_container.sh"

        print("[4/5] Uploading build script...")
        quoted_script_path = _quote_path(remote_script_path)
        result = subprocess.run(
            ["ssh", self.config.host, f"cat > {quoted_script_path}"],
            input=build_script,
            text=True,
            check=False,
            capture_output=True,
        )  # type: ignore[call-overload]
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to upload build script: {result.stderr or result.stdout}"
            )

        print("[5/5] Submitting build job to SLURM...")
        job_id = self.slurm.submit(remote_script_path)

        job_meta = {
            "job_id": job_id,
            "job_type": "build",
            "output_pattern": "logs/build_%j.out",
            "error_pattern": "logs/build_%j.err",
            "remote_dir": remote_dir,
        }
        meta_dir = self.project_dir / ".jernerics" / "jobs"
        meta_dir.mkdir(parents=True, exist_ok=True)
        meta_file = meta_dir / f"{job_id}.json"
        meta_file.write_text(json.dumps(job_meta, indent=2))

        print(f"\nBuild job submitted: {job_id}")
        print("\nMonitor progress:")
        print(f"  jernerics logs {job_id} --follow")

        return job_id
