import subprocess
import tarfile
import tempfile
from pathlib import Path

import pathspec

from jernerics.hpc.ssh import _quote_path

DEFAULT_SCP_TIMEOUT = 300

DEFAULT_EXCLUDES = [
    ".git/",
    ".jernerics/",
    "__pycache__/",
    "*.pyc",
    "*.sif",
    ".cache/",
    "results/",
    ".venv/",
    "venv/",
    "*.egg-info/",
    ".eggs/",
    "build/",
    "dist/",
    ".mypy_cache/",
    ".ruff_cache/",
]


def _load_gitignore(project_path: Path) -> pathspec.PathSpec | None:
    gitignore_path = project_path / ".gitignore"
    if not gitignore_path.exists():
        return None
    patterns = gitignore_path.read_text().splitlines()
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def _should_include(
    rel_path: str,
    gitignore_spec: pathspec.PathSpec | None,
    default_spec: pathspec.PathSpec,
) -> bool:
    if gitignore_spec and gitignore_spec.match_file(rel_path):
        return False
    return not default_spec.match_file(rel_path)


def _collect_files(
    project_path: Path,
    gitignore_spec: pathspec.PathSpec | None,
    default_spec: pathspec.PathSpec,
) -> list[Path]:
    files = []
    for item in project_path.rglob("*"):
        if not item.is_file():
            continue
        rel_path = item.relative_to(project_path).as_posix()
        if _should_include(rel_path, gitignore_spec, default_spec):
            files.append(item)
    return files


class FileSyncer:
    def __init__(self, ssh_client, remote_dir: str):
        self.ssh = ssh_client
        self.remote_dir = remote_dir.rstrip("/")

    def sync_project(
        self,
        project_dir: str | Path,
        dry_run: bool = False,
    ) -> bool:
        project_path = Path(project_dir)

        gitignore_spec = _load_gitignore(project_path)
        default_spec = pathspec.PathSpec.from_lines("gitignore", DEFAULT_EXCLUDES)

        files_to_sync = _collect_files(project_path, gitignore_spec, default_spec)

        if not files_to_sync:
            return True

        self.ssh.mkdir(self.remote_dir)

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                for file_path in files_to_sync:
                    arcname = file_path.relative_to(project_path)
                    tar.add(file_path, arcname=str(arcname))

            if dry_run:
                print(f"Would sync {len(files_to_sync)} files")
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
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=DEFAULT_SCP_TIMEOUT,
            check=False,
        )
        return result.returncode == 0

    def download_file(
        self, remote_path: str, local_path: str | Path | None = None
    ) -> bool:
        local_path = Path(remote_path).name if local_path is None else Path(local_path)

        scp_cmd = [
            "scp",
            f"{self.ssh.host}:{_quote_path(remote_path)}",
            str(local_path),
        ]
        result = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=DEFAULT_SCP_TIMEOUT,
            check=False,
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
