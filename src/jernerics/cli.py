import os
import subprocess
import time

import typer
from typing_extensions import Annotated

from ._cli_helpers import NoExperimentsFound, get_script_path, load_config

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")

run_app = typer.Typer()
app.add_typer(run_app, name="run", help="Train models.")


@run_app.command("local")
def train_run(
    config_file: Annotated[str, typer.Argument(help="Path to the configuration file.")],
    results_dir: Annotated[
        str, typer.Argument(help="Directory to store results.")
    ] = "results",
):
    """
    Run the training process directly.
    """
    try:
        _, num_experiments = load_config(config_file)
    except NoExperimentsFound as e:
        print(e)
        return

    job_script = get_script_path("run_experiment.sh")
    train_script = get_script_path("train.py", "jernerics.experiment")
    start_time = int(time.time())
    command = [
        str(job_script),
        str(train_script),
        config_file,
        str(start_time),
        results_dir,
    ]

    print("Submitting job with command:", " ".join(command))
    my_env = os.environ.copy()
    for i in range(1, num_experiments + 1):
        my_env["SLURM_ARRAY_TASK_ID"] = str(i)
        print(f"Running experiment {i}/{num_experiments}")
        p = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=my_env
        )
        if p.stdout is None or p.stderr is None:
            print("Failed to capture output.")
            return

        for line in iter(p.stdout.readline, b""):
            print(line.decode(), end="")

        for line in iter(p.stderr.readline, b""):
            print(line.decode(), end="")

        p.wait()

    cleanup_script = get_script_path("cleanup_experiment.py")
    cleanup_command = [
        cleanup_script,
        results_dir,
        f"{results_dir}/final_results.json",
    ]
    cleanup_result = subprocess.run(cleanup_command, capture_output=True, text=True)
    if cleanup_result.returncode == 0:
        print(cleanup_result.stdout)


@run_app.command("slurm")
def submit_slurm(
    config_file: Annotated[str, typer.Argument(help="Path to the configuration file.")],
    results_dir: Annotated[
        str, typer.Argument(help="Directory to store results.")
    ] = "results",
):
    """
    Submit a training job to a Slurm cluster.
    """
    try:
        _, num_experiments = load_config(config_file)
    except NoExperimentsFound as e:
        print(e)
        return

    job_script = get_script_path("run_experiment.sh")
    train_script = get_script_path("train.py", "jernerics.experiment")
    command = [
        "sbatch",
        "--parsable",
        "--array=1-{}%10".format(num_experiments),
        str(job_script),
        str(train_script),
        config_file,
        str(int(time.time())),
        results_dir,
    ]
    print("Submitting job with command:", " ".join(command))
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode == 0:
        array_job_id = result.stdout.strip()

        cleanup_script = get_script_path("cleanup_experiment.py")
        cleanup_command = [
            "sbatch",
            f"--dependency=afterok:{array_job_id}",
            cleanup_script,
            results_dir,
            f"{results_dir}/final_results.json",
        ]
        cleanup_result = subprocess.run(cleanup_command, capture_output=True, text=True)

        if cleanup_result.returncode == 0:
            print(cleanup_result.stdout)
        else:
            print(cleanup_result.stderr)

    else:
        print(result.stderr)


def main():
    app()
