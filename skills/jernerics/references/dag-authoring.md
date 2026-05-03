# DAG Authoring

## Basic pattern

Use `with DAG() as dag:` to auto-register decorated tasks:

```python
from jernerics.dag import DAG, task

with DAG() as dag:

    @task
    def load_data(config):
        return {"data": [1, 2, 3]}

    @task(depends_on=[load_data])
    def train(load_data, config):
        return {"loss": 0.05}

    @task(depends_on=[train])
    def evaluate(train, config):
        return {"accuracy": 1.0 - train["loss"]}
```

## Dependency injection

Dependencies are injected by parameter name matching:

```python
@task(depends_on=[load_data])
def train(load_data, config):
    # load_data receives the return value of the load_data task
    ...
```

**Rules:**
- `config` is always injected — contains the current trial's hyperparameters
- Return dicts from tasks — they're passed to downstream tasks
- Tasks without dependencies run in parallel (thread pool)
- If a task fails, downstream tasks are skipped; independent branches still run

## Tracker protocol

Inject `Tracker` to log metrics and artifacts:

```python
from jernerics.tracking.tracker import Tracker

@task
def evaluate(train, config, tracker: Tracker):
    tracker.log_metric("accuracy", 0.95)
    tracker.log_artifact("summary.txt", "/path/to/summary.txt")
    return {"accuracy": 0.95}
```

`Tracker` methods:
- `log_param(key, value)` — log a parameter
- `log_metric(key, value, step=None)` — log a metric
- `log_result(key, value)` — log a result
- `log_artifact(key, local_path)` — register an artifact for upload

## Path handling

Use `paths.cache_dir()` for ephemeral storage. The container sees `/work`
(project source) and `/cache` (ephemeral data).

Never hardcode host paths in generated scripts.

## Serial execution

For debugging with pdb or libraries incompatible with threading:

```python
from jernerics.dag.executor import SyncRunner
runner = SyncRunner()
```

Set in config.py or pass to `dag.run()`.
