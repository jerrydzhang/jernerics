import optuna

# Basic sweep: TPE sampler, no crashes.
# Tests: trial execution, optuna, tracking stream, artifact logging.

base = {"seed": 42}


def search_space(trial):
    return {
        "lr": trial.suggest_float("lr", 1e-4, 1e-1, log=True),
        "dropout": trial.suggest_float("dropout", 0.0, 1.0),
    }


n_trials = 5
sampler = optuna.samplers.TPESampler(seed=42)
objective = lambda results: results["loss"]
direction = "minimize"

backend_overrides = {
    "hpc": {
        "partition": "priority",
        "time": "0:10:00",
        "mem": "4G",
        "max_parallel": 10,
    },
}
