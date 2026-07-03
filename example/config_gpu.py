import optuna

base = {"seed": 42}


def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-4, 1e-1, log=True),
        "dropout": trial.suggest_float("dropout", 0.0, 1.0),
    }


n_trials = 1
sampler = optuna.samplers.TPESampler(seed=42)
objective = lambda results: results["loss"]
direction = "minimize"

backend_overrides = {
    "hpc": {
        "partition": "priority-gpu",
        "time": "0:10:00",
        "mem": "4G",
        "gres": "gpu:1",
    },
}
