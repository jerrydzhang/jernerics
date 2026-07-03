# Trial Authoring

## Basic pattern

A trial is a plain Python function `trial(config, tracker) -> dict` defined in `trial.py`:

```python
def trial(config, tracker):
    data = load_data(config["seed"])
    model = train(data, lr=config["lr"])
    accuracy = evaluate(model)

    tracker.log_metric("accuracy", accuracy)
    tracker.log_artifact("model", "model.pt")
    return {"loss": 1.0 - accuracy}
```

The runner builds `config = {**base, **search_space(trial), "config_index": n}` and calls `trial(config, tracker)`. The returned dict is passed to the config's `objective` lambda (e.g. `lambda results: results["loss"]`).

## Config

`config` is a dict holding the current trial's hyperparameters:

- `base` — fixed params from `config.py`
- sampled params from `search_space(trial)` in `config.py`
- `config_index` — the trial number (0-based)

Access values by key: `config["lr"]`, `config["seed"]`, `config["config_index"]`.

## Tracker protocol

`tracker` records what matters during the run:

```python
def trial(config, tracker):
    tracker.log_param("lr", config["lr"])
    tracker.log_metric("loss", 0.05, step=100)
    tracker.log_result("summary", {"accuracy": 0.95, "lr": config["lr"]})
    tracker.log_artifact("model", "model.pt")
    return {"loss": 0.05}
```

`Tracker` methods:

- `log_param(key, value)` — log a parameter (bool/int/float/str)
- `log_metric(key, value, step=None)` — log a metric; call repeatedly with increasing `step` for time-series; metrics stream live to the server
- `log_result(key, value)` — log a structured result (any JSON-serializable value)
- `log_artifact(key, local_path)` — register a file artifact for upload

## Return value

Return a dict whose keys the `objective` lambda reads. There is no task graph and no parallelism within a trial — the function runs linearly, top to bottom. If you need concurrent sub-steps, run them inside `trial` with your own threads/processes.

## Path handling

Use `paths.cache_dir()` for ephemeral storage. The container sees `/work`
(project source) and `/cache` (ephemeral data).

Never hardcode host paths in generated scripts.
