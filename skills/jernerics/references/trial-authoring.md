# Trial Authoring

## Basic pattern

A trial is a **top-level script** (`trial.py`), not a function. It pulls its
config and tracker from jernerics, runs, and reports results:

```python
from jernerics import trial_config, trial_tracker

config = trial_config({"lr": 0.01, "seed": 42})
tracker = trial_tracker()

data = load_data(config["seed"])
model = train(data, lr=config["lr"])
accuracy = evaluate(model)

tracker.log_value("accuracy", accuracy, step=0)
tracker.log_artifact("model", "model.pt")
tracker.finish({"loss": 1.0 - accuracy})
```

`trial_config(defaults)` returns the merged hyperparameters for this trial.
`defaults` is **required** when you run the script standalone
(`python trial.py`) and is ignored inside a jernerics job, where the runner
injects the real config.

`tracker.finish(results)` is how results reach the sweep's `objective` lambda.
There is no return value — the runner reads the `results` dict you pass to
`finish()` back from the tracking file and feeds it to `objective`.

## How the runner calls your trial

The runner does not import or call a function. For each trial it:

1. Samples params and merges `config = {**base, **search_space(trial), "config_index": n}`.
2. Writes that config to a JSON file and points `JERNERICS_TRIAL_CONFIG` at it.
3. Executes `trial.py` as a subprocess (`python trial.py`).
4. Reads the `results` dict your `tracker.finish()` wrote and passes it to the
   config's `objective` (e.g. `lambda results: results["loss"]`).

Because context flows through environment variables, your trial needs no
special entry point — just call `trial_config()` / `trial_tracker()` at the top.

## Config

`config` is a dict holding the current trial's hyperparameters:

- `base` — fixed params from `config.py`
- sampled params from `search_space(trial)` in `config.py`
- `config_index` — the trial number (0-based)

Access values by key: `config["lr"]`, `config["seed"]`, `config["config_index"]`.

## Sweep Configuration

Define the sweep at the `config.py` level. Two modes:

**`grid`** — a dict of lists, for deterministic grid sweeps:

```python
grid = {
    "pool_file": ["pools/a.pkl", "pools/b.pkl"],
    "lr": [0.01, 0.001],
}
```

Jernerics computes the cartesian product and adjusts `n_trials` automatically.
Each combination is tried exactly once. No Optuna randomness.

**`search_space`** — a callable receiving an Optuna trial, for sampled sweeps:

```python
def search_space(trial):
    return {
        "lr": trial.suggest_categorical("lr", [0.01, 0.001]),
    }
```

Called once per trial. Use when you want Optuna's sampler to guide the search.

A common mistake is assigning a plain dict to `search_space`:

```python
# WRONG — plain dict crashes (TypeError: 'dict' object is not callable)
search_space = {"lr": [0.01, 0.001]}

# RIGHT — use grid for dict-of-lists
grid = {"lr": [0.01, 0.001]}
```

## Tracker protocol

`tracker = trial_tracker()` returns a tracker — `ConsoleTracker` when run
standalone (prints each observation to stdout), or a job tracker that writes
JSONL events and streams them to the tracking server inside a jernerics job.

```python
config = trial_config(...)
tracker = trial_tracker()

tracker.log_param("lr", config["lr"])
tracker.log_value("loss", 0.05, step=100)
tracker.log_value("summary", {"epoch": 5, "fold": 2})  # non-scalar -> stored as JSON
tracker.log_artifact("model", "model.pt")
tracker.finish({"loss": 0.05, "accuracy": 0.95})
```

Tracker methods:

- `log_param(key, value)` — log a parameter (`bool`/`int`/`float`/`str`)
- `log_value(key, value, *, step=None)` — log an observation. Numbers are
  stored as scalar metrics (a time-series when you pass increasing `step`);
  any other JSON-serializable value is stored as JSON. `step` is keyword-only.
- `log_artifact(key, path)` — register a file artifact for upload
- `finish(results)` — log a results dict and close the tracker. The runner
  hands `results` to the config's `objective` lambda.

Metrics stream live to the tracking server during the run when a server is
configured; a final replay guarantees delivery.

## Results and the objective

`finish(results)` takes a dict whose keys the `objective` lambda reads. There
is no task graph and no parallelism within a trial — the script runs linearly,
top to bottom. If you need concurrent sub-steps, run them inside the script
with your own threads/processes.

## Path handling

Use `paths.cache_dir()` for ephemeral storage. The container sees `/work`
(project source) and `/cache` (ephemeral data).

Never hardcode host paths in generated scripts.
