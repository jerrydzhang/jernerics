from __future__ import annotations

_base = {"input_dim": 784, "num_classes": 10}


def search_space(trial):
    return {
        "hidden_size": trial.suggest_categorical("hidden_size", [64, 128, 256, 512]),
        "num_layers": trial.suggest_int("num_layers", 1, 4),
    }


n_trials = 8
sampler = None
objective_task = None
objective_metric = None
direction = "minimize"

backend_overrides = {
    "hpc": {
        "partition": "priority",
        "time": "0:05:00",
        "mem": "2G",
    },
}
max_workers = 2
executor_type = "thread"

