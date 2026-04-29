import optuna

# lr=1e-4 triggers permanent crash. Same params on retry → same crash.
# No transient crashes (crash_on_trials is empty).
base = {"seed": 42}

grid = {
    "lr": [1e-4, 1e-3, 1e-2],
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
