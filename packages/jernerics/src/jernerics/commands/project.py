import shutil
import subprocess
from pathlib import Path
from typing import Annotated

import tomli_w
import tomllib
import typer

from jernerics.config import ExitCode
from jernerics.container.templates import get_starter, list_starters


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


_TRIAL_SCAFFOLD = """from jernerics import trial_config, trial_tracker

config = trial_config({"loss": 0.5})
tracker = trial_tracker()

loss = config["loss"]
tracker.log_value("loss", loss, step=0)
tracker.finish({"loss": loss})
"""

_CONFIG_SCAFFOLD = """base = {"loss": 0.5}

n_trials = 3
objective = lambda results: results["loss"]
direction = "minimize"
"""
# ── init ─────────────────────────────────────────────────────────────────────


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

    trial_path = project_path / "trial.py"
    if trial_path.exists():
        print("Skipped: trial.py (already exists)")
    else:
        trial_path.write_text(_TRIAL_SCAFFOLD)
        print("Created: trial.py")

    sweep_path = project_path / "config.py"
    if sweep_path.exists():
        print("Skipped: config.py (already exists)")
    else:
        sweep_path.write_text(_CONFIG_SCAFFOLD)
        print("Created: config.py")

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
    print("  2. Run 'jernerics local trial.py config.py' to test the scaffold")
    print("  3. Run 'jernerics backend build --backend <name>' to build on remote")


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


def register(app: typer.Typer) -> None:
    app.command("init")(init)
