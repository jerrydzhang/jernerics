import optuna

# All lr values are ≥ 1e-3, so no permanent failures.
# Trials 1 and 4 crash transiently (by trial number).
# Retries get new trial numbers → succeed.
base = {"seed": 42, "crash_on_trials": [1, 4]}

grid = {
    "lr": [1e-3, 1e-2, 1e-1],
    "dropout": [0.3, 0.7],
}


def search_space(trial):
    return {
        "lr": trial.suggest_categorical("lr", grid["lr"]),
        "dropout": trial.suggest_categorical("dropout", grid["dropout"]),
    }


n_trials = 6
sampler = optuna.samplers.GridSampler(grid)
objective = lambda results: results["evaluate"]["loss"]
direction = "minimize"

slurm = {
    "partition": "priority",
    "time": "0:05:00",
    "mem": "2G",
    "max_parallel": 4,
}
