import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import pathspec

from jernerics.sync.exclusions import (
    compile_excludes,
    project_excludes,
    should_include,
)

DEFAULT_SCP_TIMEOUT = 300
MANIFEST_FILENAME = ".jernerics-sync-manifest"
DELETE_BATCH_SIZE = 100


def _quote_path(path: str) -> str:
    """Quote a path for shell, preserving ~ expansion."""
    if path.startswith("~"):
        return "~" + shlex.quote(path[1:])
    return shlex.quote(path)


def _is_safe_manifest_entry(entry: str) -> bool:
    path = PurePosixPath(entry)
    if path.is_absolute() or not path.parts:
        return False
    return ".." not in path.parts


def _collect_files(project_path: Path, spec: pathspec.PathSpec) -> list[Path]:
    files = []
    for item in project_path.rglob("*"):
        if not item.is_file():
            continue
        rel_path = item.relative_to(project_path).as_posix()
        if should_include(rel_path, spec):
            files.append(item)
    return files


class ProjectSync:
    def __init__(self, host, remote_dir: str):
        self.host = host
        self.remote_dir = remote_dir.rstrip("/")

    def sync_project(
        self,
        project_dir: str | Path,
        dry_run: bool = False,
    ) -> bool:
        project_path = Path(project_dir)

        spec = compile_excludes(project_excludes(project_path))
        files_to_sync = _collect_files(project_path, spec)

        if not files_to_sync:
            return True

        current = {
            file_path.relative_to(project_path).as_posix()
            for file_path in files_to_sync
        }
        manifest_path = f"{self.remote_dir}/{MANIFEST_FILENAME}"
        prior = self._read_prior_manifest(manifest_path)
        stale = sorted(prior - current)

        if dry_run:
            print(f"Would sync {len(files_to_sync)} files")
            print(f"Would delete {len(stale)} stale file(s)")
            return True

        self.host.mkdir(self.remote_dir)
        self._delete_stale(stale)

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                for file_path in files_to_sync:
                    arcname = file_path.relative_to(project_path)
                    tar.add(file_path, arcname=str(arcname))

            remote_tar_path = f"{self.remote_dir}/sync.tar.gz"
            scp_cmd = [
                "scp",
                tmp_path,
                f"{self.host.host}:{_quote_path(remote_tar_path)}",
            ]
            subprocess.run(scp_cmd, check=True, timeout=DEFAULT_SCP_TIMEOUT)

            quoted_dir = _quote_path(self.remote_dir)
            result = self.host.run(
                [f"cd {quoted_dir} && tar xzf sync.tar.gz"],
                check=False,
            )
            self.host.run([f"rm -f {quoted_dir}/sync.tar.gz"])
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to extract tar archive: {result.stderr or result.stdout}"
                )

            self.host.write_file(manifest_path, "\n".join(sorted(current)) + "\n")
            return True
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _read_prior_manifest(self, manifest_path: str) -> set[str]:
        content = self.host.read_file(manifest_path)
        if not content:
            return set()
        return {line.strip() for line in content.splitlines() if line.strip()}

    def _delete_stale(self, stale: list[str]) -> None:
        deletable: list[str] = []
        for entry in stale:
            if _is_safe_manifest_entry(entry):
                deletable.append(entry)
            else:
                print(f"skipped unsafe manifest entry: {entry}", file=sys.stderr)

        quoted_dir = _quote_path(self.remote_dir)
        for start in range(0, len(deletable), DELETE_BATCH_SIZE):
            batch = deletable[start : start + DELETE_BATCH_SIZE]
            quoted = " ".join(shlex.quote(entry) for entry in batch)
            command = f"cd {quoted_dir} && rm -f -- {quoted}"
            result = self.host.shell(command, check=False)
            if result.returncode != 0:
                raise RuntimeError(
                    f"Failed to delete stale files "
                    f"(exit {result.returncode}): {command}"
                )

    def container_exists(self) -> bool:
        return self.host.file_exists(f"{self.remote_dir}/container.sif")
