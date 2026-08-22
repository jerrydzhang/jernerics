"""Tier-3 e2e for the hpc backend (Slurm + Apptainer, priority partition).

The reachability gate (compute-node -> login-node server) runs inside the
``hpc_server`` fixture, so these tests only execute once it has passed.
"""

from ._helpers import (
    HPC,
    HPC_CACHE,
    fetch_artifact,
    first_artifact,
    metric_max,
    retry_ledger,
    run_sweep,
    value_count,
    wait_for,
    wait_for_settled,
    wait_for_trial_end,
)


def test_basic(hpc_server):
    """config.py: 5 trials via Slurm array + Apptainer — tracking + artifact."""
    server = hpc_server
    study = run_sweep(server, "hpc", "config.py")
    assert wait_for_trial_end(server, study, 5, timeout=600), (
        f"expected 5 terminal trials for {study}"
    )
    art = wait_for(lambda: first_artifact(server, study), timeout=60)
    assert art, f"no artifact recorded for {study}"
    _trial_id, key, artifact_id = art
    body = wait_for(lambda: fetch_artifact(server, artifact_id) or None, timeout=60)
    assert body, f"artifact {key} empty or missing for {study}"


def test_retry(hpc_server):
    """config_retry_node: staleness detection + Slurm resubmission."""
    server = hpc_server
    study = run_sweep(server, "hpc", "config_retry_node.py")
    settled = wait_for_settled(server, study, min_count=4, timeout=900)
    assert settled and settled >= 4, f"retry sweep did not settle for {study}"
    assert value_count(server, study) > 0, f"no values streamed for {study}"
    ledger = wait_for(lambda: retry_ledger(HPC, HPC_CACHE, study), timeout=30)
    assert ledger, f"no retry ledger for {study} — checker did not detect stale trials"


def test_gpu(hpc_server):
    """config_gpu: 1 trial on the priority-gpu partition with gres gpu:1."""
    server = hpc_server
    study = run_sweep(server, "hpc", "config_gpu.py")
    assert wait_for_trial_end(server, study, 1, timeout=600), (
        f"expected 1 terminal trial for {study}"
    )
    cuda = wait_for(lambda: metric_max(server, study, "cuda_available"), timeout=60)
    assert cuda, f"cuda_available not 1.0 for {study} (got {cuda})"
