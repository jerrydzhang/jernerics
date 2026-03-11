import os
import subprocess

import typer
from typing_extensions import Annotated

from ._cli_helpers import NoConfigsFound, get_script_path, load_config

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
):
    dag_path = os.path.abspath(dag_file)
    config_path = os.path.abspath(config_file)

    try:
        _, configs = load_config(config_path)
    except NoConfigsFound as e:
        print(e)
        return

    num_configs = len(configs)

    for i in range(num_configs):
        print(f"Running config {i + 1}/{num_configs}", flush=True)
        env = os.environ.copy()
        env["JERNERICS_CONFIG_INDEX"] = str(i)

        result = subprocess.run(
            ["python", "-c", _get_runner_code(dag_path, config_path, i)],
            cwd=os.path.dirname(dag_path) or ".",
            env=env,
        )

        if result.returncode != 0:
            print(f"Config {i + 1} failed with code {result.returncode}")


@run_app.command("slurm")
def run_slurm(
    dag_file: Annotated[str, typer.Argument(help="Path to the DAG file.")],
    config_file: Annotated[str, typer.Argument(help="Path to the config file.")],
    results_dir: Annotated[
        str, typer.Option("--results-dir", "-r", help="Directory to store results.")
    ] = "results",
    set_opt: Annotated[
        list[str], typer.Option("--set", "-S", help="Set SLURM option (key=value)")
    ] = [],
):
    dag_path = os.path.abspath(dag_file)
    config_path = os.path.abspath(config_file)
    results_path = os.path.abspath(results_dir)

    try:
        config_slurm, configs = load_config(config_path)
    except NoConfigsFound as e:
        print(e)
        return

    num_configs = len(configs)

    cli_overrides = {}
    for opt in set_opt:
        if "=" not in opt:
            print(f"Invalid --set option: {opt}. Expected format: key=value")
            return
        key, value = opt.split("=", 1)
        cli_overrides[key] = value

    slurm_opts = {**DEFAULT_SLURM, **config_slurm, **cli_overrides}

    max_parallel = slurm_opts.pop("max_parallel", 10)

    if max_parallel > 0:
        array_spec = f"1-{num_configs}%{max_parallel}"
    else:
        array_spec = f"1-{num_configs}"

    sbatch_args = ["sbatch", "--parsable", f"--array={array_spec}"]

    for key, value in slurm_opts.items():
        sbatch_args.extend([f"--{key}", str(value)])

    run_script = get_script_path("run_dag.sh")
    sbatch_args.extend([run_script, dag_path, config_path, results_path])

    print("Submitting job:", " ".join(sbatch_args))
    result = subprocess.run(sbatch_args, capture_output=True, text=True)

    if result.returncode == 0:
        print(f"Submitted job array: {result.stdout.strip()}")
    else:
        print(f"Failed to submit job: {result.stderr}")


def _get_runner_code(dag_file: str, config_file: str, config_index: int) -> str:
    return f'''
import os
import sys
import pathlib

dag_file = os.environ.get("JERNERICS_DAG_FILE", "{dag_file}")
config_file = os.environ.get("JERNERICS_CONFIG_FILE", "{config_file}")
config_index = int(os.environ.get("JERNERICS_CONFIG_INDEX", "{config_index}"))

dag_dir = pathlib.Path(dag_file).parent
if str(dag_dir) not in sys.path:
    sys.path.insert(0, str(dag_dir))

from jernerics.dag import DAG
from jernerics._cli_helpers import load_config

dag = DAG(dag_file)
slurm_opts, configs = load_config(config_file)
config = configs[config_index]

results = dag.run(config, config_index=config_index)

failed = [name for name, result in results.items() if isinstance(result, Exception)]
if failed:
    print("DAG failed. Tasks with errors:", ", ".join(failed))
    sys.exit(1)
else:
    print("DAG completed")
'''


def main():
    app()
