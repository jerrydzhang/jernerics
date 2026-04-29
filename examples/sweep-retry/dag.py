import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jernerics.dag import DAG, task
from sweep_retry import crash_app, crash_node, fake_loss

with DAG() as dag:

    @task
    def generate_data(config):
        trial_number = config.get("config_index", 0)

        if crash_node(trial_number, config.get("crash_node_on", [])):
            pass  # os._exit already called, won't reach here

        if crash_app(trial_number, config.get("crash_app_on", [])):
            raise RuntimeError(f"App crash: trial {trial_number}")

        return {"seed": config["seed"], "n_samples": 1000}

    @task(depends_on=[generate_data])
    def train(generate_data, config):
        loss = fake_loss(config["lr"], config["dropout"], config["seed"])
        return {"loss": loss, "lr": config["lr"], "dropout": config["dropout"]}

    @task(depends_on=[train])
    def evaluate(train, config):
        return {"loss": train["loss"], "accuracy": 1.0 - min(train["loss"], 1.0)}
