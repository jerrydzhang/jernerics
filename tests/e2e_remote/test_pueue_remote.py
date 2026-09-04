"""Tier-3 e2e for the pueue-remote backend (scimlab, Docker + GPU).

Three configs run sequentially against one co-located server (module-scoped
``scimlab_server``). See ``_helpers.py`` for the wire protocol details.
"""

import json
import re
import subprocess

from ._helpers import (
    SCIMLAB,
    SCIMLAB_CACHE,
    fetch_artifact,
    first_artifact,
    metric_max,
    retry_ledger,
    run_sweep,
    ssh,
    value_count,
    wait_for,
    wait_for_settled,
    wait_for_trial_end,
)


def _remote_task_started(study):
    res = ssh(SCIMLAB, "pueue status --json", capture_output=True, text=True)
    if res.returncode != 0:
        return None
    try:
        tasks = json.loads(res.stdout).get("tasks", {})
    except json.JSONDecodeError:
        return None
    for task in tasks.values():
        status = task.get("status", {})
        if task.get("group") in (study, f"{study}_checker") and "Queued" not in status:
            return True
    return None


def _run_cli(*args, timeout):
    return subprocess.run(
        ["jernerics", *args],
        cwd="example",
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_basic(scimlab_server):
    """config.py: 5 trials, TPE, no crashes — execution + tracking + artifact."""
    server = scimlab_server
    study = run_sweep(server, "pueue-remote", "config.py")
    assert wait_for_trial_end(server, study, 5, timeout=600), (
        f"expected 5 terminal trials for {study}"
    )
    art = wait_for(lambda: first_artifact(server, study), timeout=60)
    assert art, f"no artifact recorded for {study}"
    _trial_id, key, artifact_id = art
    body = wait_for(lambda: fetch_artifact(server, artifact_id) or None, timeout=60)
    assert body, f"artifact {key} empty or missing for {study}"


def test_retry(scimlab_server):
    """config_retry_node: 2 of 6 trials die via os._exit — staleness + resubmit."""
    server = scimlab_server
    study = run_sweep(server, "pueue-remote", "config_retry_node.py")
    settled = wait_for_settled(server, study, min_count=4, timeout=900)
    assert settled and settled >= 4, f"retry sweep did not settle for {study}"
    assert value_count(server, study) > 0, f"no values streamed for {study}"
    ledger = wait_for(lambda: retry_ledger(SCIMLAB, SCIMLAB_CACHE, study), timeout=30)
    assert ledger, f"no retry ledger for {study} — checker did not detect stale trials"


def test_gpu(scimlab_server):
    """config_gpu: 1 trial under docker --gpus all — CUDA passthrough."""
    server = scimlab_server
    study = run_sweep(server, "pueue-remote", "config_gpu.py")
    assert wait_for_trial_end(server, study, 1, timeout=600), (
        f"expected 1 terminal trial for {study}"
    )
    cuda = wait_for(lambda: metric_max(server, study, "cuda_available"), timeout=60)
    assert cuda, f"cuda_available not 1.0 for {study} (got {cuda})"


def test_job_logs_follow(scimlab_server):
    """Group-id follow ends with a per-task state line on pueue."""
    server = scimlab_server
    study = run_sweep(server, "pueue-remote", "config_gpu.py")
    assert wait_for(lambda: _remote_task_started(study), timeout=300), (
        f"no task started for {study}"
    )
    proc = _run_cli(
        "job", "logs", study, "--backend", "pueue-remote", "--follow", timeout=900
    )
    assert proc.returncode == 0, proc.stderr
    assert "--- job " in proc.stdout
    assert "follow ended ---" in proc.stdout


def test_job_resources(scimlab_server):
    """Resources dispatch through the pueue backend: wall-time, no cpu/mem."""
    server = scimlab_server
    study = run_sweep(server, "pueue-remote", "config_gpu.py")
    assert wait_for_trial_end(server, study, 1, timeout=900), (
        f"expected 1 terminal trial for {study}"
    )
    proc = _run_cli("job", "resources", study, "--backend", "pueue-remote", timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "job_id" in proc.stdout
    assert re.search(r"wall_time_s\s+\d", proc.stdout)
    assert "COMPLETED" in proc.stdout
    assert "cpu_time_s  —" in proc.stdout
