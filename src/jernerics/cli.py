import os
import subprocess
import sys

import typer
from typing_extensions import Annotated

from ._cli_helpers import (
    NoConfigsFound,
    NoContainerFound,
    find_container,
    get_script_path,
    load_config,
)

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")

run_app = typer.Typer()
app.add_typer(run_app, name="run", help="Run DAG experiments.")

DEFAULT_SLURM = {
    "output": ".cache/array_%A_%a.out",
    "error": ".cache/array_%A_%a.err",
    "max_parallel": 10,
}


@run_app.command("local")
def run_local(
    dag_file: Annotated[str, typer.Argument(help="Path to the DAG file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    results_dir: Annotated[
        str, typer.Option("--results-dir", "-r", help="Directory to store results.")
    ] = "results",
    container: Annotated[
        str | None,
        typer.Option(
            "--container", "-c", help="Path to container tarball or .sif file"
        ),
    ] = None,
    gpu: Annotated[
        bool,
        typer.Option("--gpu/--no-gpu", help="Enable GPU support via --nv flag"),
    ] = True,
):
    dag_path = os.path.abspath(dag_file)
    config_path = os.path.abspath(config_file)
    results_path = os.path.abspath(results_dir)

    try:
        _, configs, _ = load_config(config_path)
    except NoConfigsFound as e:
        print(e)
        return

    num_configs = len(configs)
    any_failed = False

    if container:
        run_script = get_script_path("run_with_container.sh")
        container_path = os.path.abspath(container)

        for i in range(num_configs):
            print(f"Running config {i + 1}/{num_configs}", flush=True)
            result = subprocess.run(
                [
                    run_script,
                    container_path,
                    dag_path,
                    config_path,
                    results_path,
                    str(gpu).lower(),
                    str(i),
                ],
            )
            if result.returncode != 0:
                print(f"Config {i + 1} failed with code {result.returncode}")
                any_failed = True
    else:
        for i in range(num_configs):
            print(f"Running config {i + 1}/{num_configs}", flush=True)
            env = os.environ.copy()
            env["JERNERICS_CONFIG_INDEX"] = str(i)

            result = subprocess.run(
                [
                    "python",
                    "-c",
                    _get_runner_code(dag_path, config_path, i, None),
                ],
                cwd=os.path.dirname(dag_path) or ".",
                env=env,
            )

            if result.returncode != 0:
                print(f"Config {i + 1} failed with code {result.returncode}")
                any_failed = True

    if any_failed:
        sys.exit(1)


@run_app.command("slurm")
def run_slurm(
    dag_file: Annotated[str, typer.Argument(help="Path to the DAG file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    results_dir: Annotated[
        str, typer.Option("--results-dir", "-r", help="Directory to store results.")
    ] = "results",
    container: Annotated[
        str | None,
        typer.Option("--container", "-c", help="Path to Apptainer .sif file"),
    ] = None,
    no_container: Annotated[
        bool,
        typer.Option(
            "--no-container", help="Run without container (use system Python)"
        ),
    ] = False,
    set_opt: Annotated[
        list[str], typer.Option("--set", "-S", help="Set SLURM option (key=value)")
    ] = [],
    print_script: Annotated[
        bool,
        typer.Option(
            "--print-script", help="Print job script to stdout instead of submitting"
        ),
    ] = False,
    bind_dir: Annotated[
        str | None,
        typer.Option("--bind-dir", help="Host directory to bind (for container jobs)"),
    ] = None,
):
    dag_path = os.path.abspath(dag_file)
    config_path = os.path.abspath(config_file)
    results_path = os.path.abspath(results_dir)
    dag_dir = os.path.dirname(dag_path)

    try:
        config_slurm, configs, _ = load_config(config_path)
    except NoConfigsFound as e:
        print(e)
        return

    num_configs = len(configs)

    try:
        container_path = find_container(container, no_container, dag_dir)
    except NoContainerFound as e:
        print(e)
        return

    cli_overrides = {}
    for opt in set_opt:
        if "=" not in opt:
            print(f"Invalid --set option: {opt}. Expected format: key=value")
            return
        key, value = opt.split("=", 1)
        cli_overrides[key] = value

    slurm_opts = {**DEFAULT_SLURM, **config_slurm, **cli_overrides}

    max_parallel = slurm_opts.pop("max_parallel", 10)
    max_parallel_val = int(max_parallel) if max_parallel else 0

    if max_parallel_val > 0:
        array_spec = f"1-{num_configs}%{max_parallel_val}"
    else:
        array_spec = f"1-{num_configs}"

    if container_path:
        run_script = get_script_path("run_dag_container.sh")
        script_args = [run_script, container_path, dag_path, config_path, results_path]
    else:
        run_script = get_script_path("run_dag.sh")
        script_args = [run_script, dag_path, config_path, results_path]

    if print_script:
        print("#!/usr/bin/env bash")
        print("#SBATCH --parsable")
        print(f"#SBATCH --array={array_spec}")
        for key, value in slurm_opts.items():
            print(f"#SBATCH --{key}={value}")
        print()

        if container_path:
            dag_dir = os.path.dirname(dag_path)
            dag_basename = os.path.basename(dag_path)
            config_basename = os.path.basename(config_path)
            container_abs = os.path.abspath(container_path)
            host_bind_dir = os.path.abspath(bind_dir) if bind_dir else dag_dir

            print("CONFIG_INDEX=$((SLURM_ARRAY_TASK_ID - 1))")
            print("export JERNERICS_DAG_FILE=/work/" + dag_basename)
            print("export JERNERICS_CONFIG_FILE=/work/" + config_basename)
            print("export JERNERICS_CONFIG_INDEX=$CONFIG_INDEX")
            print("export JERNERICS_CONTAINER=" + container_abs)
            print(
                f'apptainer exec --contain --nv --pwd /work --bind "{host_bind_dir}:/work" "{container_abs}" \\'
            )
            print("    python -c \"$(cat <<'EOF'")
            print("import os")
            print("import sys")
            print("import pathlib")
            print()
            print('dag_file = os.environ["JERNERICS_DAG_FILE"]')
            print('config_file = os.environ["JERNERICS_CONFIG_FILE"]')
            print('config_index = int(os.environ["JERNERICS_CONFIG_INDEX"])')
            print('container_path = os.environ.get("JERNERICS_CONTAINER")')
            print()
            print("sys.path.insert(0, str(pathlib.Path(dag_file).parent))")
            print()
            print("from jernerics.dag import DAG")
            print("from jernerics._cli_helpers import load_config")
            print()
            print("dag = DAG(dag_file)")
            print("slurm_opts, configs, max_workers = load_config(config_file)")
            print("config = configs[config_index]")
            print()
            print(
                "results = dag.run(config, config_index=config_index, config_path=config_file, container_path=container_path, max_workers=max_workers)"
            )
            print()
            print(
                "failed = [name for name, result in results.items() if isinstance(result, Exception)]"
            )
            print("if failed:")
            print('    print("DAG failed. Tasks with errors:", ", ".join(failed))')
            print("    sys.exit(1)")
            print("else:")
            print('    print("DAG completed")')
            print("EOF")
            print(')"')
        else:
            print("CONFIG_INDEX=$((SLURM_ARRAY_TASK_ID - 1))")
            print("export JERNERICS_DAG_FILE=" + dag_path)
            print("export JERNERICS_CONFIG_FILE=" + config_path)
            print("export JERNERICS_CONFIG_INDEX=$CONFIG_INDEX")
            print()
            print("python -c \"$(cat <<'EOF'")
            print("import os")
            print("import sys")
            print("import pathlib")
            print()
            print('dag_file = os.environ["JERNERICS_DAG_FILE"]')
            print('config_file = os.environ["JERNERICS_CONFIG_FILE"]')
            print('config_index = int(os.environ["JERNERICS_CONFIG_INDEX"])')
            print()
            print("sys.path.insert(0, str(pathlib.Path(dag_file).parent))")
            print()
            print("from jernerics.dag import DAG")
            print("from jernerics._cli_helpers import load_config")
            print()
            print("dag = DAG(dag_file)")
            print("slurm_opts, configs, max_workers = load_config(config_file)")
            print("config = configs[config_index]")
            print()
            print(
                "results = dag.run(config, config_index=config_index, config_path=config_file, max_workers=max_workers)"
            )
            print()
            print(
                "failed = [name for name, result in results.items() if isinstance(result, Exception)]"
            )
            print("if failed:")
            print('    print("DAG failed. Tasks with errors:", ", ".join(failed))')
            print("    sys.exit(1)")
            print("else:")
            print('    print("DAG completed")')
            print("EOF")
            print('"')
        return

    sbatch_args = ["sbatch", "--parsable", f"--array={array_spec}"]
    for key, value in slurm_opts.items():
        sbatch_args.extend([f"--{key}", str(value)])
    sbatch_args.extend(script_args)

    print("Submitting job:", " ".join(sbatch_args))
    result = subprocess.run(sbatch_args, capture_output=True, text=True)

    if result.returncode == 0:
        print(f"Submitted job array: {result.stdout.strip()}")
    else:
        print(f"Failed to submit job: {result.stderr}")


def _get_runner_code(
    dag_file: str,
    config_file: str,
    config_index: int,
    container_path: str | None = None,
) -> str:
    container_arg = repr(container_path) if container_path else "None"
    # ruff: noqa: E501
    return f'''
import os
import sys
import pathlib

dag_file = os.environ.get("JERNERICS_DAG_FILE", {dag_file!r})
config_file = os.environ.get("JERNERICS_CONFIG_FILE", {config_file!r})
config_index = int(os.environ.get("JERNERICS_CONFIG_INDEX", "{config_index}"))
container_path = {container_arg}

dag_dir = pathlib.Path(dag_file).parent
if str(dag_dir) not in sys.path:
    sys.path.insert(0, str(dag_dir))

from jernerics.dag import DAG
from jernerics._cli_helpers import load_config

dag = DAG(dag_file)
slurm_opts, configs, max_workers = load_config(config_file)
config = configs[config_index]

results = dag.run(config, config_index=config_index, config_path=config_file, container_path=container_path, max_workers=max_workers)

failed = [name for name, result in results.items() if isinstance(result, Exception)]
if failed:
    print("DAG failed. Tasks with errors:", ", ".join(failed))
    sys.exit(1)
else:
    print("DAG completed")
'''


def main():
    app()
