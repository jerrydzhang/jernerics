import json
import time
from pathlib import Path

from jernerics.dag import task


@task
def load_data(config):
    seed = config["seed"]
    output_dir = Path("results") / f"run_{seed}"
    output_dir.mkdir(parents=True, exist_ok=True)

    data = {"samples": 1000, "features": 50, "seed": seed}
    (output_dir / "data.json").write_text(json.dumps(data))

    time.sleep(1)
    return {"data_path": str(output_dir / "data.json"), "seed": seed}


@task
def preprocess_a(load_data, config):
    seed = load_data["seed"]
    output_dir = Path("results") / f"run_{seed}"

    time.sleep(2)

    preprocessed = {"normalized": True, "method": "standard"}
    (output_dir / "preprocess_a.json").write_text(json.dumps(preprocessed))

    return {"method": "a", "output": str(output_dir / "preprocess_a.json")}


@task
def preprocess_b(load_data, config):
    seed = load_data["seed"]
    output_dir = Path("results") / f"run_{seed}"

    time.sleep(1)

    preprocessed = {"normalized": True, "method": "minmax"}
    (output_dir / "preprocess_b.json").write_text(json.dumps(preprocessed))

    return {"method": "b", "output": str(output_dir / "preprocess_b.json")}


@task(depends_on=[preprocess_a])
def train_model_a(preprocess_a, load_data, config):
    seed = load_data["seed"]
    lr = config["lr"]
    output_dir = Path("results") / f"run_{seed}"

    time.sleep(2)

    model = {
        "lr": lr,
        "preprocess": preprocess_a["method"],
        "accuracy": 0.85 + seed * 0.01,
    }
    (output_dir / "model_a.json").write_text(json.dumps(model))

    return {"model": "a", "accuracy": model["accuracy"]}


@task(depends_on=[preprocess_b])
def train_model_b(preprocess_b, load_data, config):
    seed = load_data["seed"]
    lr = config["lr"]
    output_dir = Path("results") / f"run_{seed}"

    time.sleep(3)

    model = {
        "lr": lr,
        "preprocess": preprocess_b["method"],
        "accuracy": 0.82 + seed * 0.01,
    }
    (output_dir / "model_b.json").write_text(json.dumps(model))

    return {"model": "b", "accuracy": model["accuracy"]}


@task(depends_on=[train_model_a, train_model_b])
def compare_models(train_model_a, train_model_b, config):
    seed = config["seed"]
    output_dir = Path("results") / f"run_{seed}"

    time.sleep(1)

    best = "a" if train_model_a["accuracy"] > train_model_b["accuracy"] else "b"
    comparison = {
        "model_a_accuracy": train_model_a["accuracy"],
        "model_b_accuracy": train_model_b["accuracy"],
        "best_model": best,
    }
    (output_dir / "comparison.json").write_text(json.dumps(comparison))

    return comparison


@task(depends_on=[compare_models])
def finalize(compare_models, config):
    seed = config["seed"]
    output_dir = Path("results") / f"run_{seed}"

    summary = {
        "seed": seed,
        "best_model": compare_models["best_model"],
        "final_accuracy": max(
            compare_models["model_a_accuracy"], compare_models["model_b_accuracy"]
        ),
        "status": "completed",
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    return summary
