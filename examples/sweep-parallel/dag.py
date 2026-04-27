import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jernerics.dag import DAG, task
from sweep_parallel import fake_loss

with DAG() as dag:

    @task
    def generate_data(config):
        return {"seed": config["seed"], "n_samples": 1000}

    @task(depends_on=[generate_data])
    def train(generate_data, config):
        loss = fake_loss(config["lr"], config["dropout"], config["seed"])
        return {"loss": loss, "lr": config["lr"], "dropout": config["dropout"]}

    @task(depends_on=[train])
    def evaluate(train, config):
        return {"loss": train["loss"], "accuracy": 1.0 - min(train["loss"], 1.0)}
