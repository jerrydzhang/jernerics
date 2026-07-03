import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import tomli_w
import tomllib
import typer
from rich.console import Console
from rich.table import Table

from .backend.backend import Backend
from .backend.host import LocalHost, SSHHost
from .backend.local_backend import LocalBackend
from .backend.models import SweepSubmission
from .backend.project_sync import ProjectSync
from .config import (
    ConfigNotFound,
    ExitCode,
    find_pyproject_dir,
    get_project_name,
    load_backend_config,
    load_config,
    load_tracking_server,
)
from .container.templates import get_starter, list_starters
from .paths import cache_dir

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")


SAFE_RELPATH = re.compile(r"^[a-zA-Z0-9_./\-]+$")


def _validate_relpath(path: str, desc: str) -> str:
    if not SAFE_RELPATH.match(path):
        raise SystemExit(
            f"Error: {desc} path '{path}' contains unsafe characters. "
            "Only alphanumeric, underscore, hyphen, period, and slash allowed."
        )
    if ".." in path:
        raise SystemExit(
            f"Error: {desc} path '{path}' must not contain '..' (path traversal)."
        )
    return path


def _get_backend(backend_name: str) -> tuple[Backend, str, Path]:
    """Load a backend by name. Returns (backend, project_name, project_dir)."""
    from .backend.factory import make_backend

    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        config = load_backend_config(backend_name, project_dir)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_name = get_project_name(project_dir)
    tracking_server = load_tracking_server(project_dir)

    remote_dir = config.shared.remote_dir.replace("{project_name}", project_name)
    remote_dir = remote_dir.replace("{project-name}", project_name)

    if config.shared.host:
        host = SSHHost(config.shared.host)
        syncer = ProjectSync(host, remote_dir)
    else:
        host = LocalHost()
        syncer = None

    backend = make_backend(
        config,
        host=host,
        syncer=syncer,
        tracking_server=tracking_server,
        project_name=project_name,
    )

    return backend, project_name, project_dir


# ── run local ────────────────────────────────────────────────────────────────


@app.command("local")
def run_local(
    trial_file: Annotated[str, typer.Argument(help="Path to the trial file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
):
    trial_path = Path(trial_file).resolve()
    config_path = Path(config_file).resolve()

    if not trial_path.exists():
        print(f"Error: trial file not found: {trial_path}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        sweep = load_config(str(config_path))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_dir = find_pyproject_dir()
    project_name = get_project_name(project_dir) if project_dir else None
    tracking_server = load_tracking_server(project_dir) if project_dir else None

    project_cache = cache_dir()
    optuna_dir = project_cache / "optuna"
    optuna_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"local_{config_path.stem}_{timestamp}"
    storage_path = str(optuna_dir / (study_name + ".journal"))

    spec = SweepSubmission(
        trial_path=trial_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_path,
        n_trials=sweep.n_trials,
        project_name=project_name,
        server_addr=tracking_server,
        grid=sweep.grid,
    )

    backend = LocalBackend(tracking_server=tracking_server)

    try:
        backend.submit_sweep(spec, direction=sweep.direction)
    except RuntimeError:
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── run (remote) ─────────────────────────────────────────────────────────────


@app.command("run")
def run_remote(
    trial_file: Annotated[str, typer.Argument(help="Path to the trial file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    set_opt: Annotated[
        list[str] | None,
        typer.Option("--set", "-S", help="Set backend option (key=value)"),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without submitting"),
    ] = False,
):
    if set_opt is None:
        set_opt = []
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    trial_path = Path(trial_file).resolve()
    config_path = Path(config_file).resolve()

    if not trial_path.exists():
        print(f"Error: trial file not found: {trial_path}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        sweep = load_config(str(config_path))
    except (FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    cli_overrides = {}
    for opt in set_opt:
        if "=" not in opt:
            print(f"Error: Invalid --set option: {opt}. Expected format: key=value")
            raise SystemExit(ExitCode.CONFIG_ERROR)
        key, value = opt.split("=", 1)
        if not key:
            print(f"Error: Empty key in --set option: {opt}")
            raise SystemExit(ExitCode.CONFIG_ERROR)
        cli_overrides[key] = value

    backend, project_name, project_dir = _get_backend(backend_name)

    trial_relpath = _validate_relpath(
        str(trial_path.relative_to(project_dir)), "trial file"
    )
    config_relpath = _validate_relpath(
        str(config_path.relative_to(project_dir)), "Config file"
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"{project_name}_{config_path.stem}_{timestamp}"
    storage_url = backend.storage_path(study_name)

    spec = SweepSubmission(
        trial_path=trial_path,
        config_path=config_path,
        study_name=study_name,
        storage_url=storage_url,
        n_trials=sweep.n_trials,
        trial_relpath=trial_relpath,
        config_relpath=config_relpath,
        project_name=project_name,
        server_addr=backend.tracking_server,
        grid=sweep.grid,
    )

    try:
        result = backend.prepare_and_submit(
            spec,
            project_dir=project_dir,
            project_name=project_name,
            direction=sweep.direction,
            dry_run=dry_run,
            backend_name=backend_name,
            experiment_overrides=sweep.backend_overrides.get(backend_name, {}),
            cli_overrides=cli_overrides,
            local_cache_dir=cache_dir(),
        )
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if result is not None:
        print("\nMonitor progress:")
        job_id = result.submissions[0].job_id
        print(f"  jernerics logs --backend {backend_name} {job_id} --follow")


# ── build ────────────────────────────────────────────────────────────────────


@app.command("build")
def build(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    project_dir: Annotated[
        str, typer.Argument(help="Project directory (default: current)")
    ] = ".",
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Force rebuild even if up to date"),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview actions without executing"),
    ] = False,
):
    project_path = Path(project_dir).resolve()
    if not project_path.exists():
        print(f"Error: Project directory not found: {project_path}")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    backend, project_name, _ = _get_backend(backend_name)

    try:
        backend.build(
            project_path,
            project_name=project_name,
            force=force,
            dry_run=dry_run,
            local_cache_dir=cache_dir(),
        )
    except (RuntimeError, FileNotFoundError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── jobs ─────────────────────────────────────────────────────────────────────


@app.command("jobs")
def jobs(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include completed jobs"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
):
    backend, _, _ = _get_backend(backend_name)
    job_list = backend.list_jobs(include_completed=all)

    if json_output:
        data = [
            {
                "job_id": job.job_id,
                "name": job.name,
                "status": job.status,
            }
            for job in job_list
        ]
        print(json.dumps(data, indent=2))
        return

    if not job_list:
        print("No jobs found.")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("JOB_ID")
    table.add_column("NAME", max_width=20)
    table.add_column("STATUS")

    for job in job_list:
        table.add_row(job.job_id, job.name, job.status)

    Console().print(table)


# ── cancel ───────────────────────────────────────────────────────────────────


@app.command("cancel")
def cancel(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    job_id: Annotated[
        str | None,
        typer.Argument(help="Job ID to cancel"),
    ] = None,
    all: Annotated[
        bool,
        typer.Option("--all", "-a", help="Cancel all your jobs"),
    ] = False,
):
    backend, _, _ = _get_backend(backend_name)

    if all:
        if backend.cancel_all():
            print("Cancelled all jobs.")
        else:
            print("Failed to cancel jobs.")
        return

    if job_id is None:
        print("Error: Specify a job ID or use --all")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    if backend.cancel(job_id):
        print(f"Cancelled job {job_id}.")
    else:
        print(f"Failed to cancel job {job_id}.")


# ── logs ─────────────────────────────────────────────────────────────────────


@app.command("logs")
def logs(
    job_id: Annotated[str, typer.Argument(help="Job ID")],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Follow log output"),
    ] = False,
    array_index: Annotated[
        int | None,
        typer.Option("--array-index", "-i", help="Array task index (for array jobs)"),
    ] = None,
    stderr: Annotated[
        bool,
        typer.Option("--stderr", "-e", help="Show stderr instead of stdout"),
    ] = False,
):
    backend, _, _ = _get_backend(backend_name)

    try:
        backend.get_logs(
            job_id,
            follow=follow,
            stderr=stderr,
            local_cache_dir=cache_dir(),
        )
    except SystemExit:
        raise
    except Exception as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── clean ────────────────────────────────────────────────────────────────────


@app.command("clean")
def clean(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    full: Annotated[
        bool,
        typer.Option("--full", help="Also delete project source and container"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Execute (dry-run by default)"),
    ] = False,
) -> None:
    backend, project_name, _ = _get_backend(backend_name)

    try:
        backend.clean(project_name, full=full, force=force)
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── sync ─────────────────────────────────────────────────────────────────────


@app.command("sync")
def sync(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    study: Annotated[
        str | None,
        typer.Option("--study", "-s", help="Scope to a single study"),
    ] = None,
):
    backend, _, project_dir = _get_backend(backend_name)
    project_name = get_project_name(project_dir)
    backend.sync(project_name, study=study)


def _copy_starter(project_path: Path, starter: str, ext: str, filename: str) -> None:
    target = project_path / filename
    if target.exists():
        print(f"Skipped: {filename} (already exists)")
        return
    try:
        content = get_starter(starter, ext=ext)
        target.write_text(content)
        print(f"Created: {filename}")
    except ValueError:
        pass


# ── init ─────────────────────────────────────────────────────────────────────


@app.command("init")
def init(
    project_dir: Annotated[
        str, typer.Argument(help="Directory to initialize (default: current)")
    ] = ".",
    starter: Annotated[
        str, typer.Option("--starter", "-s", help="Container starter to use")
    ] = "python",
    force: Annotated[
        bool,
        typer.Option(
            "--force", "-f", help="Overwrite existing [tool.jernerics] config"
        ),
    ] = False,
):
    if shutil.which("uv") is None:
        print("Error: 'uv' command not found. Please install uv first.")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    project_path = Path(project_dir).resolve()
    project_name = project_path.name

    if starter not in list_starters():
        print(
            f"Error: Unknown starter: {starter}. "
            f"Available: {', '.join(list_starters())}"
        )
        raise SystemExit(ExitCode.GENERAL_ERROR)

    project_path.mkdir(parents=True, exist_ok=True)

    pyproject_path = project_path / "pyproject.toml"
    jernerics_config = _get_default_jernerics_config(project_name)

    if pyproject_path.exists():
        try:
            with open(pyproject_path, "rb") as f:
                existing = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            print(f"Error: Malformed pyproject.toml: {e}")
            raise SystemExit(ExitCode.CONFIG_ERROR) from None

        has_jernerics = "jernerics" in existing.get("tool", {})

        if (
            has_jernerics
            and not force
            and not typer.confirm(
                "[tool.jernerics] already exists in pyproject.toml. Overwrite?",
                default=False,
            )
        ):
            print("Skipped updating pyproject.toml")
            return

        existing.setdefault("tool", {})["jernerics"] = jernerics_config
        merged = existing
    else:
        merged = _create_minimal_pyproject(project_name, jernerics_config)

    with open(pyproject_path, "wb") as f:
        tomli_w.dump(merged, f)

    print("Updated: pyproject.toml")

    _copy_starter(project_path, starter, ".def", "container.def")
    _copy_starter(project_path, starter, ".Dockerfile", "Dockerfile")

    src_dir = project_path / "src"
    if not src_dir.exists():
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "__init__.py").write_text('"""Project source."""\n')
        print("Created: src/")
    else:
        print("Skipped: src/ (already exists)")

    uv_result = subprocess.run(
        ["uv", "sync"],
        cwd=project_path,
        capture_output=True,
        check=False,
        text=True,
    )
    if uv_result.returncode == 0:
        print("Generated: uv.lock")
    else:
        print(f"Warning: 'uv sync' failed: {uv_result.stderr.strip()}")

    print(f"\nProject initialized in {project_path}")
    print("\nNext steps:")
    print("  1. Edit pyproject.toml to add dependencies")
    print(
        "  2. Create your trial and config files "
        "(e.g., experiments/trial.py, configs/default.py)"
    )
    print("  3. Run 'jernerics build --backend <name>' to build on remote")


def _get_default_jernerics_config(project_name: str) -> dict:
    return {
        "backends": {
            "hpc": {
                "type": "slurm",
                "host": "your-username@hpc.example.edu",
                "remote_dir": f"~/experiments/{project_name}",
                "cache_dir": "/scratch/$USER/jernerics",
                "slurm": {
                    "partition": "priority",
                    "time": "1:00:00",
                    "mem": "16G",
                    "cpus": 4,
                },
            }
        }
    }


def _create_minimal_pyproject(project_name: str, jernerics_config: dict) -> dict:
    return {
        "project": {
            "name": project_name,
            "version": "0.1.0",
            "description": "Add description here",
            "requires-python": ">=3.12",
            "dependencies": ["jernerics"],
        },
        "tool": {
            "uv": {
                "sources": {
                    "jernerics": {"git": "https://github.com/jerrydzhang/jernerics.git"}
                }
            },
            "jernerics": jernerics_config,
        },
        "build-system": {
            "requires": ["hatchling"],
            "build-backend": "hatchling.build",
        },
    }


def main():
    app()
