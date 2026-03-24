import subprocess
import tarfile
import tempfile
from pathlib import Path

from jernerics.hpc.ssh import _quote_path

DEFAULT_SCP_TIMEOUT = 300


def _safe_tar_filter(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if tarinfo.islnk() or tarinfo.issym():
        return None
    if tarinfo.name.startswith("/") or ".." in tarinfo.name:
        return None
    return tarinfo


class FileSyncer:
    def __init__(self, ssh_client, remote_dir: str):
        self.ssh = ssh_client
        self.remote_dir = remote_dir.rstrip("/")

    DEFAULT_FILES = [
        "pyproject.toml",
        "uv.lock",
        "container.def",
        "dag.py",
        "config.py",
    ]
    DEFAULT_DIRS = ["src"]

    def sync_project(
        self,
        project_dir: str | Path,
        exclude_patterns: list[str] | None = None,
        dry_run: bool = False,
        files: list[str] | None = None,
        dirs: list[str] | None = None,
    ) -> bool:
        project_path = Path(project_dir)

        if exclude_patterns is None:
            exclude_patterns = [
                "*.pyc",
                "__pycache__",
                "*.sif",
                ".git",
                ".cache",
                "results",
                ".jernerics",
            ]

        self.ssh.mkdir(self.remote_dir)

        files_to_sync = files if files is not None else self.DEFAULT_FILES
        dirs_to_sync = dirs if dirs is not None else self.DEFAULT_DIRS

        existing_files = [f for f in files_to_sync if (project_path / f).exists()]
        existing_dirs = [d for d in dirs_to_sync if (project_path / d).is_dir()]

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                for f in existing_files:
                    tar.add(project_path / f, arcname=f, filter=_safe_tar_filter)
                for d in existing_dirs:
                    tar.add(project_path / d, arcname=d, filter=_safe_tar_filter)

            if dry_run:
                print(f"Would sync: {existing_files + existing_dirs}")
                return True

            remote_tar_path = f"{self.remote_dir}/sync.tar.gz"
            scp_cmd = [
                "scp",
                tmp_path,
                f"{self.ssh.host}:{_quote_path(remote_tar_path)}",
            ]
            subprocess.run(scp_cmd, check=True, timeout=DEFAULT_SCP_TIMEOUT)

            quoted_dir = _quote_path(self.remote_dir)
            result = self.ssh.run(
                f"cd {quoted_dir} && tar xzf sync.tar.gz",
                check=False,
            )
            self.ssh.run(f"rm -f {quoted_dir}/sync.tar.gz")
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to extract tar archive: {result.stderr or result.stdout}"
                )

            return True
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def sync_file(self, local_path: str | Path, remote_path: str | None = None) -> bool:
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        if remote_path is None:
            remote_path = f"{self.remote_dir}/{local_path.name}"

        scp_cmd = [
            "scp",
            str(local_path),
            f"{self.ssh.host}:{_quote_path(remote_path)}",
        ]
        result = subprocess.run(
            scp_cmd, capture_output=True, text=True, timeout=DEFAULT_SCP_TIMEOUT
        )
        return result.returncode == 0

    def download_file(
        self, remote_path: str, local_path: str | Path | None = None
    ) -> bool:
        if local_path is None:
            local_path = Path(remote_path).name
        else:
            local_path = Path(local_path)

        scp_cmd = [
            "scp",
            f"{self.ssh.host}:{_quote_path(remote_path)}",
            str(local_path),
        ]
        result = subprocess.run(
            scp_cmd, capture_output=True, text=True, timeout=DEFAULT_SCP_TIMEOUT
        )
        return result.returncode == 0

    def container_exists(self) -> bool:
        return self.ssh.file_exists(f"{self.remote_dir}/container.sif")

    def container_needs_rebuild(self, local_lock_path: str | Path) -> bool:
        if not self.container_exists():
            return True

        remote_mtime = self.ssh.getmtime(f"{self.remote_dir}/container.sif")
        if remote_mtime is None:
            return True

        local_path = Path(local_lock_path)
        if not local_path.exists():
            return True

        local_mtime = local_path.stat().st_mtime
        return local_mtime > remote_mtime
