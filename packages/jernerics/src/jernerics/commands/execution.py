import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from jernerics.backend.local_backend import LocalBackend
from jernerics.backend.models import SweepSubmission
from jernerics.backend.pueue.adapter import PueueSubmitError
from jernerics.backend.slurm.adapter import SlurmSubmitError
from jernerics.commands.common import _get_backend
from jernerics.config import (
    ConfigValidationError,
    ExitCode,
    find_pyproject_dir,
    get_project_name,
    load_config,
    load_tracking_server,
)
from jernerics.paths import cache_dir
from jernerics.tracking.infra import TrackingServerSchemeError

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


def _coerce_param_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _capture_git_hash(cwd: Path | None) -> str | None:
    """Best-effort git commit hash for sweep provenance; None if not a git repo."""
    if cwd is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(cwd),
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
    ):
        return None
    return result.stdout.strip() or None


# ── run local ────────────────────────────────────────────────────────────────


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
    except (ConfigValidationError, FileNotFoundError, RuntimeError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_dir = find_pyproject_dir()
    project_name = get_project_name(project_dir) if project_dir else None
    tracking_server = load_tracking_server() if project_dir else None
    git_hash = _capture_git_hash(project_dir or trial_path.parent)

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
        git_hash=git_hash,
    )

    backend = LocalBackend(tracking_server=tracking_server)

    try:
        backend.submit_sweep(spec, direction=sweep.direction)
    except TrackingServerSchemeError as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None
    except RuntimeError:
        raise SystemExit(ExitCode.GENERAL_ERROR) from None


# ── run (remote) ─────────────────────────────────────────────────────────────


def run_remote(
    trial_file: Annotated[str, typer.Argument(help="Path to the trial file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    set_opt: Annotated[
        list[str] | None,
        typer.Option("--set", "-S", help="Set scheduler override (key=value)"),
    ] = None,
    set_param_opt: Annotated[
        list[str] | None,
        typer.Option("--set-param", help="Set trial-config param (key=value)"),
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
    except (ConfigValidationError, FileNotFoundError, RuntimeError) as e:
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

    param_overrides = {}
    for opt in set_param_opt or []:
        if "=" not in opt:
            print(
                f"Error: Invalid --set-param option: {opt}. Expected format: key=value"
            )
            raise SystemExit(ExitCode.CONFIG_ERROR)
        key, raw = opt.split("=", 1)
        if not key:
            print(f"Error: Empty key in --set-param option: {opt}")
            raise SystemExit(ExitCode.CONFIG_ERROR)
        param_overrides[key] = _coerce_param_value(raw)

    backend, project_name, project_dir = _get_backend(backend_name)

    valid_keys = backend.adapter.valid_override_keys()
    unknown = set(cli_overrides) - valid_keys
    if unknown:
        unknown_keys = ", ".join(sorted(unknown))
        valid = ", ".join(sorted(valid_keys))
        print(
            f"Error: Unknown override key(s) for backend '{backend_name}': "
            f"{unknown_keys}. Valid keys: {valid}"
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    trial_relpath = _validate_relpath(
        str(trial_path.relative_to(project_dir)), "trial file"
    )
    config_relpath = _validate_relpath(
        str(config_path.relative_to(project_dir)), "Config file"
    )

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    study_name = f"{project_name}_{config_path.stem}_{timestamp}"
    storage_url = backend.storage_path(study_name)
    git_hash = _capture_git_hash(project_dir)

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
        git_hash=git_hash,
        param_overrides=param_overrides,
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
    except (PueueSubmitError, SlurmSubmitError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.SLURM_ERROR) from None
    except (RuntimeError, ValueError) as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if result is not None:
        print("\nMonitor progress:")
        job_id = result.submissions[0].job_id
        print(f"  jernerics job logs --backend {backend_name} {job_id} --follow")
        print(f"  jernerics job wait --backend {backend_name} {job_id}")

        tracking_server = load_tracking_server()
        if tracking_server:
            query_hint = (
                f"  curl -X POST {tracking_server}/query"
                ' -H "Content-Type: application/json"'
                ' -d \'{"sql": "SELECT * FROM tracked_values'
                " ORDER BY timestamp_ns DESC LIMIT 5\"}'"
            )
            print("\nQuery metrics:")
            print(query_hint)


def register(app: typer.Typer) -> None:
    app.command("local")(run_local)
    app.command("run")(run_remote)
