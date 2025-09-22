import subprocess
from importlib import resources
import time

import typer
from typing_extensions import Annotated
import yaml

app = typer.Typer(help="A modern toolkit for building and evaluating ML models.")

train_app = typer.Typer()
app.add_typer(train_app, name="train", help="Train models.")

submit_app = typer.Typer()
app.add_typer(submit_app, name="submit", help="Submit jobs to a cluster.")


@train_app.command("run")
def train_run(
    model_name: Annotated[str, typer.Option(help="Name of the model to train.")],
    epochs: Annotated[int, typer.Option(help="Number of training epochs.")] = 10,
):
    """
    Run the training process directly.
    """
    print(f"Starting training for model: {model_name} for {epochs} epochs...")
    # Your call to the actual training logic would go here
    # from .training.main import start
    # start(model_name, epochs)


@submit_app.command("slurm")
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
        "--array=1-{}".format(num_experiments),
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
