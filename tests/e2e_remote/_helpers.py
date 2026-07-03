"""Helpers for tier-3 remote e2e tests.

Shared by ``conftest.py`` (fixtures) and the per-backend test modules. Nothing
here submits jobs except ``run_sweep`` / ``build_backend``; the fixtures in
``conftest.py`` own server lifecycle.
"""

import json
import os
import shlex
import subprocess
import time

# Fixed credential shared by the co-located server and the trial env. Matches
# the key hard-coded in nix/tests/module.nix.
TEST_API_KEY = "test-secret-key"

SCIMLAB = "jez21005@scimlab.engr.uconn.edu"
HPC = "jez21005@hpc2.storrs.hpc.uconn.edu"
PORT = 18080
PROJECT = "sweep-e2e"

# Host-side cache dirs (the container's /cache maps onto these). The retry
# ledger lands at <cache>/tracking/<study>/.retry_ledger.json.
SCIMLAB_CACHE = "~/.cache/jernerics"
HPC_CACHE = "/scratch/qiy18011/jez21005/jernerics"

# Server process paths (inside the container's /tmp, which both docker and
# apptainer share with the host).
SERVER_DB = "/tmp/jernerics-e2e.db"
SERVER_ARTIFACTS = "/tmp/jernerics-e2e-artifacts"
SERVER_CONTAINER = "jernerics-e2e-server"
HPC_SERVER_LOG = "/tmp/jernerics-e2e.log"


def ssh(host, cmd, **kwargs):
    return subprocess.run(["ssh", host, cmd], **kwargs)


def build_backend(backend_name):
    """Build the project image on the remote (idempotent: skips if up to date)."""
    subprocess.run(
        ["jernerics", "build", "--backend", backend_name],
        cwd="example",
        check=True,
        capture_output=True,
        text=True,
    )


# ── server queries (run via SSH curl on the remote where the server lives) ────


def query(server, sql):
    host, base_url = server
    payload = shlex.quote(json.dumps({"sql": sql}))
    res = ssh(
        host,
        f"curl -s -X POST {base_url}/query "
        f"-H 'Authorization: Bearer {TEST_API_KEY}' "
        f"-H 'Content-Type: application/json' -d {payload}",
        capture_output=True,
        text=True,
    )
    return json.loads(res.stdout).get("rows", [])


def wait_for(fn, timeout, interval=5):
    """Call fn until it returns truthy or timeout elapses; swallow transient errors."""
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        try:
            result = fn()
        except Exception:
            result = None
        if result:
            return result
        time.sleep(interval)
    return result


def wait_for_health(server, timeout=60):
    host, base_url = server
    return wait_for(
        lambda: (
            ssh(
                host,
                f"curl -s -o /dev/null -w '%{{http_code}}' {base_url}/api/health "
                f"-H 'Authorization: Bearer {TEST_API_KEY}'",
                capture_output=True,
                text=True,
            ).stdout.strip()
            == "200"
        ),
        timeout,
    )


# ── sweep execution + study discovery ────────────────────────────────────────


def studies(server):
    return {
        r[0]
        for r in query(
            server,
            f"SELECT DISTINCT study_name FROM metrics WHERE project='{PROJECT}'",
        )
    }


def run_sweep(server, backend_name, config_file, discover_timeout=180):
    """Submit a sweep and return its study_name.

    The CLI does not print the study name. Every trial logs ``cuda_available``
    to ``metrics`` as its very first action (before any crash), so the new study
    appears there once the first trial starts. We diff the study set before/after
    to recover the exact name regardless of timestamp races.
    """
    before = studies(server)
    env = {
        **os.environ,
        "JERNERICS_TRACKING_SERVER": server[1],
        "JERNERICS_API_KEY": TEST_API_KEY,
    }
    subprocess.run(
        ["jernerics", "run", "--backend", backend_name, "trial.py", config_file],
        cwd="example",
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    def new_study():
        diff = studies(server) - before
        return diff.pop() if len(diff) == 1 else None

    return wait_for(new_study, discover_timeout)


# ── assertions against the tracking server ───────────────────────────────────


def count(server, study, table):
    rows = query(
        server,
        f"SELECT COUNT(*) FROM {table} "
        f"WHERE project='{PROJECT}' AND study_name='{study}'",
    )
    return rows[0][0] if rows else 0


def wait_for_trial_end(server, study, expected, timeout):
    return wait_for(
        lambda: count(server, study, "trial_end") == expected or None, timeout
    )


def wait_for_settled(server, study, min_count, timeout, quiet=40):
    """Poll trial_end until it reaches min_count and stops growing for `quiet` s.

    Retry sweeps have a non-deterministic terminal count (retried trials may
    succeed or exhaust), so we wait for quiescence rather than an exact number.
    """
    deadline = time.time() + timeout
    last = -1
    stable_at = None
    while time.time() < deadline:
        current = count(server, study, "trial_end")
        if current >= min_count:
            if current != last:
                last, stable_at = current, time.time()
            elif time.time() - stable_at >= quiet:
                return current
        time.sleep(5)
    return last


def metric_max(server, study, key):
    rows = query(
        server,
        f"SELECT MAX(value) FROM metrics "
        f"WHERE project='{PROJECT}' AND study_name='{study}' AND key='{key}'",
    )
    return rows[0][0] if rows and rows[0][0] is not None else None


def first_artifact(server, study):
    rows = query(
        server,
        f"SELECT trial_id, key FROM artifacts "
        f"WHERE project='{PROJECT}' AND study_name='{study}' LIMIT 1",
    )
    return tuple(rows[0]) if rows else None


def fetch_artifact(server, study, trial_id, key):
    host, base_url = server
    res = ssh(
        host,
        f"curl -s -X GET {base_url}/artifact/{PROJECT}/{study}/{trial_id}/{key} "
        f"-H 'Authorization: Bearer {TEST_API_KEY}'",
        capture_output=True,
    )
    return res.stdout


def retry_ledger(host, cache_dir, study):
    """Return the retry ledger contents, or None if not (yet) written."""
    res = ssh(
        host,
        f"cat {cache_dir}/{PROJECT}/tracking/{study}/.retry_ledger.json",
        capture_output=True,
        text=True,
    )
    return res.stdout if res.returncode == 0 and res.stdout.strip() else None
