from __future__ import annotations

from jernerics.dag import DAG, task

with DAG() as dag:

    @task
    def step_a(config):
        return {"value": config.get("seed", 42) * 2}

    @task(depends_on=[step_a])
    def step_b(step_a, config):
        if config.get("should_fail", False):
            raise RuntimeError("Intentional failure for testing resume")
        return {"value": step_a["value"] + 10}

    @task(depends_on=[step_b])
    def step_c(step_b, config):
        return {"value": step_b["value"] * 3, "status": "completed"}
