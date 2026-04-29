from __future__ import annotations

import optuna

_base = {"seed": 42, "model": "mlp"}


def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-4, 1e-1, log=True),
        "dropout": trial.suggest_float("dropout", 0.0, 1.0),
    }


n_trials = 20
sampler = optuna.samplers.TPESampler(seed=42)
objective_task = "evaluate"
objective_metric = "loss"
direction = "minimize"

backend_overrides = {
    "hpc": {
        "partition": "priority",
        "time": "0:10:00",
        "mem": "4G",
    },
}
max_workers = 2
executor_type = "thread"

