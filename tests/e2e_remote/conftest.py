"""Tier-3 remote e2e: skip gate + co-located server fixtures.

Every test in this package submits real jobs to a remote backend. Tests are
SKIPPED unless ``JERNERICS_RUN_REMOTE=1`` is set, so ``just test`` never
submits anything. Run with ``just test-remote`` (both backends in parallel).

The co-located ``jernerics-server`` runs on the remote from the same project
image the trials use. Both container runtimes share the host network namespace
(Docker ``--network=host``; Apptainer host netns by default), so a trial reaches
the server at its loopback/hostname without extra bind mounts.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from ._helpers import (
    HPC,
    HPC_SERVER_LOG,
    PORT,
    PROJECT,
    SCIMLAB,
    SERVER_ARTIFACTS,
    SERVER_CONTAINER,
    SERVER_DB,
    TEST_API_KEY,
    build_backend,
    ssh,
    wait_for_health,
)

_REMOTE_DIR = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    if os.environ.get("JERNERICS_RUN_REMOTE"):
        return
    skip = pytest.mark.skip(reason="remote e2e; set JERNERICS_RUN_REMOTE=1 to run")
    for item in items:
        if item.path.is_relative_to(_REMOTE_DIR):
            item.add_marker(skip)


@pytest.fixture(scope="module")
def scimlab_server():
    """Co-located server on scimlab (docker --network=host) at 127.0.0.1:PORT."""
    build_backend("pueue-remote")
    ssh(SCIMLAB, f"docker rm -f {SERVER_CONTAINER}", capture_output=True)
    ssh(
        SCIMLAB,
        f"docker run --network=host --rm -d --name {SERVER_CONTAINER} "
        f"-e JERNERICS_API_KEY={TEST_API_KEY} "
        f"sweep-e2e python -m jernerics_server --host 0.0.0.0 --http-port {PORT} "
        f"--db {SERVER_DB} --artifacts-dir {SERVER_ARTIFACTS}",
        check=True,
        capture_output=True,
        text=True,
    )
    server = (SCIMLAB, f"http://127.0.0.1:{PORT}")
    if not wait_for_health(server):
        logs = ssh(
            SCIMLAB,
            f"docker logs {SERVER_CONTAINER}",
            capture_output=True,
            text=True,
        ).stdout
        pytest.fail(f"scimlab co-located server did not become healthy:\n{logs}")
    yield server
    ssh(SCIMLAB, f"docker stop {SERVER_CONTAINER}", capture_output=True)
    status = ssh(SCIMLAB, "pueue status --json", capture_output=True, text=True)
    if status.returncode != 0:
        return
    try:
        tasks = json.loads(status.stdout).get("tasks", {})
    except json.JSONDecodeError:
        return
    groups = {
        task["group"]
        for task in tasks.values()
        if task.get("group", "").startswith(f"{PROJECT}_")
    }
    for group in sorted(groups):
        ssh(SCIMLAB, f"pueue clean --group {group}", capture_output=True)


@pytest.fixture(scope="module")
def hpc_server():
    """Co-located server on the hpc login node (apptainer) at the FQDN:PORT.

    Trials run on compute nodes, so the URL is the login-node hostname (not
    localhost). A reachability gate sruns a probe from a compute node before any
    real work is submitted.
    """
    build_backend("hpc")
    ssh(HPC, "pkill -f jernerics_server", capture_output=True)
    ssh(HPC, f"rm -rf {SERVER_DB} {SERVER_ARTIFACTS}", capture_output=True)
    login_host = ssh(HPC, "hostname -f", capture_output=True, text=True).stdout.strip()
    subprocess.run(
        [
            "ssh",
            "-f",
            HPC,
            f"cd ~/projects/jernerics-examples/{PROJECT} && "
            f"apptainer exec --env JERNERICS_API_KEY={TEST_API_KEY} container.sif "
            f"python -m jernerics_server --host 0.0.0.0 --http-port {PORT} "
            f"--db {SERVER_DB} --artifacts-dir {SERVER_ARTIFACTS} "
            f"> {HPC_SERVER_LOG} 2>&1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )
    server = (HPC, f"http://{login_host}:{PORT}")
    if not wait_for_health(server):
        logs = ssh(HPC, f"cat {HPC_SERVER_LOG}", capture_output=True, text=True).stdout
        pytest.fail(f"hpc co-located server did not become healthy:\n{logs}")

    # Reachability gate: compute node -> login-node server.
    try:
        probe = ssh(
            HPC,
            f"srun --time=1:00 curl -s -o /dev/null -w '%{{http_code}}' "
            f"{server[1]}/api/health -H 'Authorization: Bearer {TEST_API_KEY}'",
            capture_output=True,
            text=True,
            timeout=180,
        )
        code = probe.stdout.strip()
    except subprocess.TimeoutExpired:
        code = ""
    if code != "200":
        pytest.fail(
            "hpc compute nodes cannot reach the co-located server at "
            f"{server[1]} (srun probe returned {code!r}). "
            "Aborting before submitting real work."
        )

    yield server
    ssh(HPC, "pkill -f jernerics_server", capture_output=True)
