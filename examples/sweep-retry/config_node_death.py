import optuna

# Type 2 failure: simulated node death on specific trial numbers.
# os._exit(9) kills process immediately — Optuna trial stays RUNNING,
# heartbeat file goes stale. Checker detects stale, marks FAIL,
# enqueues same params, increments ledger.
# Retries get new trial numbers → succeed.
base = {"seed": 42, "crash_node_on": [1, 4]}

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
