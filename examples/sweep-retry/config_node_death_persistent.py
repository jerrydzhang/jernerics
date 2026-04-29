import optuna

# Type 2 failure: persistent node death by param value.
# Any trial with lr < lr_fatal (5e-4) gets os._exit(9).
# Enqueued retries get the same params → also die.
# After max_retries (3), the checker gives up on those params and
# submits a fresh trial.
#
# Expected chain:
#   Round 1: lr=1e-3 (complete), lr=1e-4, crash.
#            Ledger: {key: 1}
#   Round 2: lr=1e-4 (enqueued), crash.  Ledger: {key: 2}
#   Round 3: lr=1e-4 (enqueued), crash.  Ledger: {key: 3}
#   Round 4: lr=1e-4 (enqueued), crash.  Count=3 >= max_retries. Exhausted.
#            Fresh trial submitted.
#   Round 5: lr=1e-3 (fresh). Succeeds. Chain ends.
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
objective = lambda results: results["evaluate"]["loss"]
direction = "minimize"

backend_overrides = {
    "hpc": {
        "partition": "priority",
        "time": "0:05:00",
        "mem": "2G",
        "max_parallel": 2,
    },
}

