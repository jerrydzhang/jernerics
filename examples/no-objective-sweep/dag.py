from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jernerics.dag import DAG, task
from no_objective_sweep import compute_model_stats

with DAG() as dag:

    @task
    def build_model(config):
        return {
            "hidden_size": config["hidden_size"],
            "num_layers": config["num_layers"],
        }

    @task(depends_on=[build_model])
    def profile(build_model, config):
        stats = compute_model_stats(
            hidden_size=config["hidden_size"],
            num_layers=config["num_layers"],
        )
        return stats

    @task(depends_on=[profile])
    def summarize(profile, config):
        return {
            "status": "completed",
            "params_M": profile["params_count"] / 1e6,
            "gflops": profile["flops"] / 1e9,
        }
