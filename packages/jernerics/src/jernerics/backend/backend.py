import hashlib
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import tomllib

from jernerics.backend.build_marker import needs_rebuild
from jernerics.backend.job_meta import load_job_studies, save_job_meta
from jernerics.backend.models import JobInfo, SubmitResult, SweepSubmission
from jernerics.backend.project_sync import _quote_path
from jernerics.backend.pueue.adapter import PueueAdapter, pueue_group_from_label
from jernerics.backend.submission import (
    SweepInfrastructure,
    submit_sweep,
)
from jernerics.paths import cache_dir
from jernerics.tracking.batch_sync import replay_tracking, ship_events_file
from jernerics.tracking.infra import resolve_tracking_ship


def _check_path_dependencies(project_dir: Path) -> None:
    """Fail fast if pyproject.toml declares local path deps in [tool.uv.sources].

    Path dependencies reference files outside the project tree that are not
    available inside the container build context, so `uv sync --frozen` fails.
    """
    pyproject_path = project_dir / "pyproject.toml"
    if not pyproject_path.exists():
        return

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    path_deps = [
        (name, src["path"])
        for name, src in sources.items()
        if isinstance(src, dict) and "path" in src
    ]
    if not path_deps:
        return

    lines = ["Path dependencies detected and cannot be used in container builds:"]
    for name, path in path_deps:
        lines.append(f"  {name} -> {path}")
    lines.append("")
    lines.append(
        "The referenced path is not available inside the container build context."
    )
    lines.append("Use a git dependency instead. Example:")
    first_name = path_deps[0][0]
    lines.append(f"  [tool.uv.sources.{first_name}]")
    lines.append('  git = "https://github.com/<user>/<repo>.git"')
    lines.append(f'  subdirectory = "packages/{first_name}"')
    raise RuntimeError("\n".join(lines))


def _ship_submission_events(
    host,
    remote_path: str,
    base_url: str,
    api_key: str | None,
) -> None:
    """Land the sweep's submission events on the server from the deploy side.

    The file is written on the host filesystem (remote backends: the
    cluster), so read it back over the host transport and ship a local
    copy — live trial streams need the sweep row on the server from
    their first batch. Best-effort: the post-hook replay remains the
    delivery guarantee.
    """
    try:
        content = host.read_file(remote_path)
        if content is None:
            print(
                f"jernerics: submission events not readable at {remote_path}; "
                "the post-hook replay will deliver them.",
                file=sys.stderr,
            )
            return
        with tempfile.TemporaryDirectory() as tmp_dir:
            local = Path(tmp_dir) / "submission.jsonl"
            local.write_text(content)
            ship_events_file(local, base_url, api_key)
    except Exception as exc:
        print(
            f"jernerics: immediate ship of submission events failed: {exc!r}; "
            "the post-hook replay will deliver them.",
            file=sys.stderr,
        )


class Backend:
    def __init__(
        self,
        host,
        infra: SweepInfrastructure,
        syncer,
        *,
        project_name: str,
        tracking_server: str | None = None,
        heartbeat_interval_s: float = 60.0,
        stale_after_s: int = 120,
        grace_period_s: int = 120,
        max_retries: int = 3,
        chain_depth_cap: int = 20,
    ):
        self.host = host
        self.infra = infra
        self.syncer = syncer
        self.project_name = project_name
        self.tracking_server = tracking_server
        self.heartbeat_interval_s = heartbeat_interval_s
        self.stale_after_s = stale_after_s
        self.grace_period_s = grace_period_s
        self.max_retries = max_retries
        self.chain_depth_cap = chain_depth_cap

    @property
    def container(self):
        return self.infra.container

    @property
    def adapter(self):
        return self.infra.adapter

    @property
    def paths(self):
        return self.infra.paths

    def prepare_and_submit(
        self,
        spec: SweepSubmission,
        *,
        project_dir: Path,
        project_name: str,
        direction: str,
        dry_run: bool = False,
        backend_name: str = "",
        experiment_overrides: dict[str, Any] | None = None,
        cli_overrides: dict[str, str] | None = None,
        local_cache_dir: Path | None = None,
    ) -> SubmitResult | None:
        # Pop output/error before merging — they go into job meta, not the script
        all_overrides = {
            **(experiment_overrides or {}),
            **(cli_overrides or {}),
        }
        output_pattern = all_overrides.get("output")
        error_pattern = all_overrides.get("error")

        # Build cleaned overrides for submit_sweep (exclude output/error)
        clean_experiment = {
            k: v
            for k, v in (experiment_overrides or {}).items()
            if k not in ("output", "error")
        }
        clean_cli = {
            k: v
            for k, v in (cli_overrides or {}).items()
            if k not in ("output", "error")
        }

        if dry_run:
            script = submit_sweep(
                spec,
                self.infra,
                host=self.host,
                project_dir=project_dir,
                project_name=project_name,
                backend_name=backend_name,
                direction=direction,
                tracking_server=self.tracking_server,
                cli_overrides=clean_cli or None,
                experiment_overrides=clean_experiment or None,
                heartbeat_interval_s=self.heartbeat_interval_s,
                dry_run=True,
            )
            print("=== DRY RUN ===")
            print(f"Backend: {backend_name}")
            print(f"Host: {getattr(self.host, 'host', 'local')}")
            print(f"Remote dir: {self.paths.remote_dir}")
            print()
            print("=== SCRIPT ===")
            print(script)
            return None

        # Sync
        if self.syncer is not None:
            host_label = getattr(self.host, "host", "local")
            print(f"Syncing project to {host_label}:{self.paths.remote_dir}...")
            self.syncer.sync_project(project_dir)

        # Readiness check
        cache_host = self.paths.resolve_cache()
        self.host.mkdir(f"{cache_host}/optuna")
        if self.syncer is not None:
            result = self.host.run(
                self.container.exists_command(self.paths.remote_dir),
                check=False,
                capture_output=True,
            )
            if result.returncode != 0:
                print(
                    "Error: container not found on remote.\n"
                    "  Run 'jernerics backend build --backend <name>' first."
                )
                raise RuntimeError("container not found on remote")

        # Submit via shared submission module
        result = submit_sweep(
            spec,
            self.infra,
            host=self.host,
            project_dir=project_dir,
            project_name=project_name,
            backend_name=backend_name,
            direction=direction,
            tracking_server=self.tracking_server,
            cli_overrides=clean_cli or None,
            experiment_overrides=clean_experiment or None,
            heartbeat_interval_s=self.heartbeat_interval_s,
        )

        # Land the sweep/submission/job events immediately so live trial
        # streams validate from their first batch.
        if spec.project_name and result is not None and self.tracking_server:
            ship = resolve_tracking_ship(self.tracking_server)
            if ship:
                base_url, api_key = ship
                _ship_submission_events(
                    self.host,
                    f"{cache_host}/tracking/{spec.study_name}"
                    f"/submission/{spec.submission_id}.jsonl",
                    base_url,
                    api_key,
                )

        if local_cache_dir is not None and result is not None:
            if isinstance(self.adapter, PueueAdapter):
                for sub in result.submissions:
                    save_job_meta(
                        job_id=sub.job_id,
                        study_name=spec.study_name,
                        backend="pueue",
                        remote_dir=self.paths.remote_dir,
                        n_trials=sub.n_trials,
                        local_cache_dir=local_cache_dir,
                    )
            else:
                effective_output = output_pattern or f"{cache_host}/logs/%A_%a.out"
                effective_error = error_pattern or f"{cache_host}/logs/%A_%a.err"
                for sub in result.submissions:
                    save_job_meta(
                        job_id=sub.job_id,
                        study_name=spec.study_name,
                        backend="slurm",
                        output_pattern=str(sub.output_pattern or effective_output),
                        error_pattern=str(sub.error_pattern or effective_error),
                        remote_dir=self.paths.remote_dir,
                        n_trials=sub.n_trials,
                        local_cache_dir=local_cache_dir,
                    )

        return result

    def build(
        self,
        project_dir: Path,
        *,
        project_name: str,
        force: bool = False,
        dry_run: bool = False,
        backend_name: str = "",
        local_cache_dir: Path | None = None,
    ) -> None:
        lock_path = project_dir / "uv.lock"
        if not lock_path.exists():
            raise FileNotFoundError("uv.lock not found. Run 'uv lock' first.")

        container_def_path = project_dir / "container.def"
        dockerfile_path = project_dir / "Dockerfile"
        has_build_file = container_def_path.exists() or dockerfile_path.exists()

        if not has_build_file:
            from jernerics.container.templates import generate_container_def

            container_def_path.write_text(generate_container_def("python"))
            print("Template generated: container.def")

        _check_path_dependencies(project_dir)

        cache_host = self.paths.resolve_cache()
        marker_path = f"{cache_host}/.build_marker"

        if (
            not dry_run
            and not force
            and not needs_rebuild(self.host, marker_path, lock_path, container_def_path)
        ):
            print("Container is up to date. Use --force to rebuild.")
            return

        host_label = getattr(self.host, "host", None)
        if dry_run:
            print("=== DRY RUN ===")
            print(f"Project dir: {project_dir}")
            print(f"Remote dir: {self.paths.remote_dir}")
            if host_label:
                print(f"Host: {host_label}")
            print()
            print("Would sync files and submit build job.")
            return

        self.host.mkdir(f"{cache_host}/logs")

        if self.syncer is not None:
            label = host_label or "local"
            print(f"Syncing project to {label}:{self.paths.remote_dir}...")
            self.syncer.sync_project(project_dir)

        # Compose build script
        build_cmd = self.container.build_command(self.paths.remote_dir)
        cmd_str = " ".join(shlex.quote(c) for c in build_cmd)
        build_dir = self.paths.resolve_build_dir(project_name)

        def_hash = (
            hashlib.sha256(container_def_path.read_bytes()).hexdigest()
            if container_def_path.exists()
            else ""
        )

        if build_dir is not None:
            build_script = (
                f"set -e\n"
                f"mkdir -p {build_dir}\n"
                f"export APPTAINER_TMPDIR={build_dir}\n"
                f"cd {self.paths.remote_dir}\n"
                f"{cmd_str}\n"
                f"rm -rf {build_dir}\n"
                f"mkdir -p {Path(marker_path).parent}\n"
                f"echo '{def_hash}' > {marker_path}\n"
            )
        else:
            build_script = (
                f"set -e\n"
                f"cd {self.paths.remote_dir}\n"
                f"{cmd_str}\n"
                f"mkdir -p {Path(marker_path).parent}\n"
                f"echo '{def_hash}' > {marker_path}\n"
            )

        job_id = self.adapter.submit_job(
            build_script, name="container-build", log_dir=f"{cache_host}/logs"
        )

        if job_id and local_cache_dir is not None:
            if isinstance(self.adapter, PueueAdapter):
                save_job_meta(
                    job_id=job_id,
                    backend="pueue",
                    remote_dir=self.paths.remote_dir,
                    n_trials=1,
                    local_cache_dir=local_cache_dir,
                )
            else:
                save_job_meta(
                    job_id=job_id,
                    output_pattern=f"{cache_host}/logs/build_%j.out",
                    error_pattern=f"{cache_host}/logs/build_%j.err",
                    remote_dir=self.paths.remote_dir,
                    n_trials=1,
                    local_cache_dir=local_cache_dir,
                )

        print(f"\nBuild job submitted: {job_id}")
        print("Monitor build with:")
        print(f"  jernerics job logs --backend {backend_name} {job_id} --follow")

    def clean(
        self,
        project_name: str,
        *,
        full: bool = False,
        force: bool = False,
    ) -> None:
        cache_host = self.paths.resolve_cache()

        target_desc = "cache + project directory" if full else "cache directory"
        host_label = getattr(self.host, "host", None)
        if host_label:
            print(f"Target: {target_desc} on {host_label}")
        else:
            print(f"Target: {target_desc}")
        print(f"  cache:   {cache_host}")
        if full:
            print(f"  project: {self.paths.remote_dir}")

        active = [
            j
            for j in self.list_jobs()
            if j.status
            not in (
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                "TIMEOUT",
                "STASHED",
                "LOCKED",
            )
        ]
        if active:
            print(f"\nError: {len(active)} active job(s) found. Cancel them first.")
            for j in active:
                print(f"  {j.job_id}  {j.name}  {j.status}")
            raise RuntimeError("Active jobs prevent cleaning")

        result = self.host.shell(
            f"find {cache_host}/tracking"
            " -path '*/events/*.jsonl' 2>/dev/null | head -n 1",
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print("\nError: Unsynced tracking data found. Run sync first.")
            raise RuntimeError("Unsynced tracking data")

        artifact_check_cmd = (
            f"cd {cache_host}/tracking && "
            "for m in $(find . -path '*/artifacts/*.manifest' 2>/dev/null); do "
            'c="${m%.manifest}.cursor"; '
            'ms=$(stat -c%s "$m" 2>/dev/null || echo 0); '
            'cs=$(cat "$c" 2>/dev/null || echo 0); '
            'if [ "$cs" -lt "$ms" ]; then echo "$m"; break; fi; '
            "done"
        )
        result = self.host.shell(
            artifact_check_cmd,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            print("\nError: Unsynced artifact data found. Run sync first.")
            raise RuntimeError("Unsynced artifact data")

        r = self.host.run(["test", "-d", cache_host], check=False, capture_output=True)
        if r.returncode != 0:
            print(f"\nError: cache directory '{cache_host}' not found.")
            raise FileNotFoundError(f"Cache directory not found: {cache_host}")

        if full:
            r = self.host.run(
                ["test", "-d", self.paths.remote_dir], check=False, capture_output=True
            )
            if r.returncode != 0:
                print(
                    f"\nError: project directory '{self.paths.remote_dir}' not found."
                )
                raise FileNotFoundError(
                    f"Project directory not found: {self.paths.remote_dir}"
                )

        if not force:
            print("\nDry run. Use --force to execute.")
            return

        self.adapter.cleanup()

        r = self.host.run(
            ["rm", "-rf", cache_host], check=False, capture_output=True, text=True
        )
        if r.returncode != 0:
            print(f"Failed to delete {cache_host}: {r.stderr}")
            raise RuntimeError(f"Failed to delete {cache_host}")
        print(f"Deleted: {cache_host}")

        if full:
            r = self.host.run(
                ["rm", "-rf", self.paths.remote_dir],
                check=False,
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                print(f"Failed to delete {self.paths.remote_dir}: {r.stderr}")
                raise RuntimeError(f"Failed to delete {self.paths.remote_dir}")
            print(f"Deleted: {self.paths.remote_dir}")

    def sync(
        self,
        project_name: str,
        *,
        study: str | None = None,
    ) -> None:
        ship = resolve_tracking_ship(self.tracking_server or "")
        if ship is None:
            raise RuntimeError("No tracking server configured")
        base_url, api_key = ship
        host_desc = getattr(self.host, "host", "local")
        print(f"Syncing tracking data from {host_desc}...")

        if self.host.is_local:
            tracking_dir = Path(f"{self.paths.resolve_cache()}/tracking")
        else:
            tracking_dir = self._pull_tracking_cache()

        print("Replaying events to the tracking server...")
        result = replay_tracking(
            tracking_dir=tracking_dir,
            base_url=base_url,
            api_key=api_key,
            study=study,
        )
        if result.errors:
            raise RuntimeError(
                f"Tracking replay failed for {len(result.errors)} file(s): "
                f"{result.errors[0]}"
            )
        print("Sync complete.")

    def _pull_tracking_cache(self) -> Path:
        """Pull the remote tracking cache into the local cache directory."""
        cache = self.paths.resolve_cache()
        remote_tar = f"{cache}/tracking-pull.tar.gz"
        local_cache = cache_dir()

        tar_cmd = (
            f"tar -C {shlex.quote(cache)} --exclude tracking/env"
            f" -czf {shlex.quote(remote_tar)} tracking"
        )
        tarred = self.host.shell(
            tar_cmd, timeout=600, check=False, capture_output=True, text=True
        )
        if tarred.returncode != 0:
            raise RuntimeError(
                f"Remote tar failed with exit code {tarred.returncode}: "
                f"{tar_cmd}\n{tarred.stderr or tarred.stdout}"
            )

        remote_host = getattr(self.host, "host", None)
        if remote_host is None:
            print(f"Would run: scp {remote_tar} {local_cache}/tracking-pull.tar.gz")
            return local_cache / "tracking"

        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            local_tar = tmp.name
        try:
            scp_cmd = [
                "scp",
                f"{remote_host}:{_quote_path(remote_tar)}",
                local_tar,
            ]
            copied = subprocess.run(scp_cmd, check=False, timeout=600)
            if copied.returncode != 0:
                raise RuntimeError(
                    f"scp failed with exit code {copied.returncode}: "
                    f"{' '.join(scp_cmd)}. The remote tarball remains "
                    f"at {remote_tar}"
                )

            self.host.shell(f"rm -f {shlex.quote(remote_tar)}", check=False)

            local_cache.mkdir(parents=True, exist_ok=True)
            try:
                with tarfile.open(local_tar, "r:gz") as tar:
                    tar.extractall(local_cache, filter="data")
            except (OSError, tarfile.TarError) as e:
                raise RuntimeError(
                    f"Failed to extract {local_tar} into {local_cache}: {e}"
                ) from e
        finally:
            Path(local_tar).unlink(missing_ok=True)

        return local_cache / "tracking"

    def get_logs(
        self,
        job_id: str,
        *,
        follow: bool = False,
        stderr: bool = False,
        array_index: int | None = None,
        local_cache_dir: Path | None = None,
    ) -> None:
        self.adapter.get_logs(
            job_id,
            follow=follow,
            stderr=stderr,
            array_index=array_index,
            meta={
                "local_cache_dir": local_cache_dir,
                "host": self.host,
                "cache_host": self.paths.resolve_cache(),
            },
        )

    # Delegated to adapter

    def list_jobs(
        self,
        include_completed: bool = False,
        *,
        local_cache_dir: Path | None = None,
    ) -> list[JobInfo]:
        jobs = self.adapter.list_jobs(include_completed=include_completed)
        if local_cache_dir is not None:
            studies = load_job_studies(local_cache_dir)
            for job in jobs:
                study = studies.get(job.job_id)
                if study is None and "_" in job.job_id:
                    study = studies.get(job.job_id.split("_")[0])
                if study is None:
                    group = pueue_group_from_label(job.name)
                    if group is not None:
                        study = studies.get(group)
                job.study_name = study or ""
        return jobs

    def cancel(self, job_id: str) -> bool:
        return self.adapter.cancel(job_id)

    def cancel_all(self) -> bool:
        return self.adapter.cancel_all()

    def get_status(self, job_id: str) -> str | None:
        return self.adapter.get_status(job_id)

    def wait_for_completion(
        self, job_id: str, poll_interval: float = 30, timeout: float | None = None
    ) -> bool:
        return self.adapter.wait_for_completion(job_id, poll_interval, timeout)

    def storage_path(self, study_name: str) -> str:
        return self.paths.storage_path(study_name)

    def cleanup(self) -> None:
        self.adapter.cleanup()
