import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jernerics.tracking.tracker import Tracker
from sweep_e2e import crash_app, crash_node, fake_loss


def trial(config, tracker: Tracker) -> dict:
    # detect_gpu
    try:
        import torch
    except ImportError:
        tracker.log_metric("cuda_available", 0.0)
    else:
        cuda = torch.cuda.is_available()
        tracker.log_metric("cuda_available", float(cuda))

    # generate_data
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

    # train
    loss = fake_loss(config["lr"], config["dropout"], config["seed"])

    # evaluate
    accuracy = 1.0 - min(loss, 1.0)

    out_dir = Path("artifacts-out")
    out_dir.mkdir(exist_ok=True)
    trial_idx = config.get("config_index", 0)
    summary_file = out_dir / f"summary-{trial_idx}.txt"
    summary_file.write_text(
        f"Trial {trial_idx}: loss={loss:.4f}, "
        f"lr={config['lr']}, dropout={config['dropout']}, "
        f"accuracy={accuracy:.4f}\n"
    )
    tracker.log_artifact(f"summary-{trial_idx}.txt", str(summary_file))
    tracker.log_result(
        "summary",
        {
            "loss": loss,
            "accuracy": accuracy,
            "lr": config["lr"],
            "dropout": config["dropout"],
        },
    )

    return {"loss": loss, "accuracy": accuracy}
