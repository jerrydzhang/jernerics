# Grid sweep + param-driven persistent failure.
# Any trial with lr < lr_fatal (5e-4) gets os._exit(9).
# Retried trials get same params → also die → max_retries exhausted.

base = {"seed": 42, "lr_fatal": 5e-4}

grid = {
    "lr": [1e-3, 1e-4],
    "dropout": [0.3],
}


def search_space(trial):
    return {
        "lr": trial.suggest_categorical("lr", [1e-3, 1e-4]),
        "dropout": trial.suggest_categorical("dropout", [0.3]),
    }


n_trials = 2
objective = lambda results: results["loss"]
direction = "minimize"

backend_overrides = {
    "hpc": {
        "partition": "priority",
        "time": "0:05:00",
        "mem": "2G",
        "max_parallel": 2,
    },
}
