import os
import subprocess
import time
from importlib import resources

import typer
import yaml
from typing_extensions import Annotated

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
    with open(config_file, "r") as f:
        config_data = yaml.safe_load(f)

    num_experiments = len(config_data.get("experiments", []))

    if num_experiments == 0:
        print("No experiments found in the configuration file.")
        return

    job_script = resources.files("jernerics.scripts").joinpath("run_experiment.sh")
    train_script = resources.files("jernerics.experiment").joinpath("train.py")
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
        # result = subprocess.run(command, capture_output=True, text=True, env=my_env)
        p = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=my_env
        )
        for line in iter(p.stdout.readline, b""):
            print(line.decode(), end="")
        for line in iter(p.stderr.readline, b""):
            print(line.decode(), end="")

        p.wait()
        # if result.returncode == 0:
        #     print("Job completed successfully.")
        #     print(result.stdout)
        # else:
        #     print("Job failed.")
        #     print(result.stdout)
        #     print(result.stderr)


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
    with open(config_file, "r") as f:
        config_data = yaml.safe_load(f)

    num_experiments = len(config_data.get("experiments", []))

    if num_experiments == 0:
        print("No experiments found in the configuration file.")
        return

    job_script = resources.files("jernerics.scripts").joinpath("run_experiment.sh")
    train_script = resources.files("jernerics.experiment").joinpath("train.py")
    command = [
        "sbatch",
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
        print("Job submitted successfully.")
        print(result.stdout)
    else:
        print("Failed to submit job.")
        print(result.stdout)
        print(result.stderr)


def main():
    app()
