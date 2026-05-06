import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jernerics.dag import DAG, task
from jernerics.tracking.tracker import Tracker
from sweep_e2e import crash_app, crash_node, fake_loss

with DAG() as dag:

    @task
    def detect_gpu(tracker: Tracker):
        try:
            import torch
        except ImportError:
            tracker.log_metric("cuda_available", 0.0)
            return {"cuda_available": False, "device": "torch-not-installed"}

        cuda = torch.cuda.is_available()
        device = torch.cuda.get_device_name(0) if cuda else "cpu-only"
        tracker.log_metric("cuda_available", float(cuda))
        return {"cuda_available": cuda, "device": device}

    @task
    def generate_data(config, tracker: Tracker):
        tracker.log_param("seed", config["seed"])
        tracker.log_param("n_samples", 1000)
        trial_number = config.get("config_index", 0)

        crash_node(
            trial_number,
            config.get("crash_node_on", []),
            lr=config.get("lr", 0),
            lr_fatal=config.get("lr_fatal", -1),
        )

        if crash_app(trial_number, config.get("crash_app_on", [])):
            raise RuntimeError(f"App crash: trial {trial_number}")

        return {"seed": config["seed"], "n_samples": 1000}

    @task(depends_on=[generate_data])
    def train(generate_data, config):
        loss = fake_loss(config["lr"], config["dropout"], config["seed"])
        return {"loss": loss, "lr": config["lr"], "dropout": config["dropout"]}

    @task(depends_on=[train])
    def evaluate(train, config, tracker: Tracker):
        accuracy = 1.0 - min(train["loss"], 1.0)

        out_dir = Path("artifacts-out")
        out_dir.mkdir(exist_ok=True)
        trial_idx = config.get("config_index", 0)
        summary_file = out_dir / f"summary-{trial_idx}.txt"
        summary_file.write_text(
            f"Trial {trial_idx}: loss={train['loss']:.4f}, "
            f"lr={train['lr']}, dropout={train['dropout']}, "
            f"accuracy={accuracy:.4f}\n"
        )
        tracker.log_artifact(f"summary-{trial_idx}.txt", str(summary_file))
        tracker.log_result(
            "summary",
            {
                "loss": train["loss"],
                "accuracy": accuracy,
                "lr": train["lr"],
                "dropout": train["dropout"],
            },
        )

        return {"loss": train["loss"], "accuracy": accuracy}
