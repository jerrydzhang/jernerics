from pathlib import Path
from typing import Annotated

import typer

from jernerics.commands.common import _get_backend
from jernerics.config import ExitCode
from jernerics.paths import cache_dir

# ── build ────────────────────────────────────────────────────────────────────


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


# ── clean ────────────────────────────────────────────────────────────────────


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


def register(app: typer.Typer) -> None:
    app.command("build")(build)


# Registered after the job commands in cli.py to keep the original help order
def register_clean(app: typer.Typer) -> None:
    app.command("clean")(clean)
