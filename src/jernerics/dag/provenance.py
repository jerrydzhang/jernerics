from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _get_git_sha(repo_path: Path | None = None) -> str | None:
    if repo_path is None:
        return None
    if not repo_path.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _get_file_hash(file_path: Path) -> str | None:
    if not file_path.exists():
        return None
    try:
        content = file_path.read_bytes()
        return "sha256:" + hashlib.sha256(content).hexdigest()
    except OSError:
        return None


def _resolve_container_path(container_path: str | None) -> dict[str, Any] | None:
    if not container_path:
        return None

    path = Path(container_path)
    if not path.exists():
        return {"path": container_path}

    resolved = path.resolve()
    if str(resolved).startswith("/nix/store/"):
        return {
            "path": str(path),
            "store_path": str(resolved),
        }
    return {"path": str(path)}


def _get_slurm_job_id() -> str | None:
    return os.environ.get("SLURM_JOB_ID")


def _get_jernerics_version() -> str:
    try:
        from importlib.metadata import version

        return version("jernerics")
    except Exception:
        return "unknown"


@dataclass
class Provenance:
    run_id: str
    jernerics_version: str
    git_sha: str | None
    config: dict[str, Any]
    python: str
    platform: str
    container: dict[str, Any] | None
    slurm_job_id: str | None
    started_at: str
    ended_at: str | None = None

    @classmethod
    def create(
        cls,
        run_id: str,
        config_path: str | None = None,
        container_path: str | None = None,
        repo_path: Path | None = None,
    ) -> Provenance:
        config_info: dict[str, Any] = {}
        if config_path:
            config_file = Path(config_path)
            config_info = {
                "path": str(config_file),
                "hash": _get_file_hash(config_file),
            }

        return cls(
            run_id=run_id,
            jernerics_version=_get_jernerics_version(),
            git_sha=_get_git_sha(repo_path),
            config=config_info,
            python=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            platform=platform.platform(),
            container=_resolve_container_path(container_path),
            slurm_job_id=_get_slurm_job_id(),
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def finalize(self) -> None:
        self.ended_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, state_dir: Path) -> Path:
        runs_dir = state_dir / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)

        provenance_file = runs_dir / f"{self.run_id}_provenance.json"
        temp_file = runs_dir / f".tmp_{self.run_id}_provenance.json"
        try:
            with open(temp_file, "w") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(temp_file, provenance_file)
        except Exception:
            temp_file.unlink(missing_ok=True)
            raise

        return provenance_file

    @classmethod
    def from_json(cls, path: Path) -> Provenance:
        with open(path) as f:
            data = json.load(f)
        return cls(
            run_id=data["run_id"],
            jernerics_version=data["jernerics_version"],
            git_sha=data.get("git_sha"),
            config=data.get("config", {}),
            python=data.get("python", "unknown"),
            platform=data.get("platform", "unknown"),
            container=data.get("container"),
            slurm_job_id=data.get("slurm_job_id"),
            started_at=data["started_at"],
            ended_at=data.get("ended_at"),
        )
