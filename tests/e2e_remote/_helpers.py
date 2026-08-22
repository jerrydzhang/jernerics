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
from datetime import UTC, datetime
from pathlib import Path

# Fixed credential shared by the co-located server and the trial env. Matches
# the key hard-coded in nix/tests/module.nix.
TEST_API_KEY = "test-secret-key"

SCIMLAB = "jez21005@scimlab.engr.uconn.edu"
HPC = "jez21005@hpc2.storrs.hpc.uconn.edu"
PORT = 18080
PROJECT = "sweep-e2e"

# Host-side cache dirs (the container's /cache maps onto these). The retry
# ledger lands at <cache>/tracking/<sweep>/.retry_ledger.json.
SCIMLAB_CACHE = "~/.cache/jernerics"
HPC_CACHE = "/scratch/qiy18011/jez21005/jernerics"

# Server process paths (inside the container's /tmp, which both docker and
# apptainer share with the host).
SERVER_DB = "/tmp/jernerics-e2e.db"
SERVER_ARTIFACTS = "/tmp/jernerics-e2e-artifacts"
SERVER_CONTAINER = "jernerics-e2e-server"
HPC_SERVER_LOG = "/tmp/jernerics-e2e.log"

# Terminal v3 trial states (jernerics_schema.TrialState); a trial in one of
# these states never changes again.
TERMINAL_TRIAL_STATES = ("completed", "failed", "pruned")


def ssh(host, cmd, **kwargs):
    return subprocess.run(["ssh", host, cmd], **kwargs)


def _build_state(backend_name):
    """Opaque remote-side image fingerprint and build-marker mtime.

    The build job rewrites the marker when it finishes, so a changed state
    means "build completed" even when a fully cached docker build produces a
    byte-identical image.
    """
    if backend_name == "hpc":
        host, cache = HPC, HPC_CACHE
        image = f"stat -c %Y ~/projects/jernerics-examples/{PROJECT}/container.sif"
    else:
        host, cache = SCIMLAB, SCIMLAB_CACHE
        image = f"docker image inspect {PROJECT} --format '{{{{.Created}}}}'"
    marker = f"stat -c %Y {cache}/{PROJECT}/.build_marker"
    res = ssh(
        host,
        f"{image} 2>/dev/null; echo -n marker:; {marker} 2>/dev/null",
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def build_backend(backend_name):
    """Build the project image on the remote.

    Idempotent: an up-to-date image skips the build entirely. Otherwise the
    build runs as an async scheduler job, so poll the remote until the build
    completes — the fixtures start the co-located server from the image right
    after, and a stale image silently runs old code.
    """
    before = _build_state(backend_name)
    res = subprocess.run(
        ["jernerics", "backend", "build", "--backend", backend_name],
        cwd="example",
        check=True,
        capture_output=True,
        text=True,
    )
    if "up to date" in res.stdout:
        return
    timeout = 1800 if backend_name == "hpc" else 900
    if not wait_for(lambda: _build_state(backend_name) != before, timeout, interval=15):
        raise RuntimeError(f"{backend_name} image build did not complete")


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


# ── sweep execution + sweep discovery ────────────────────────────────────────


def studies(server):
    return {
        r[0]
        for r in query(
            server,
            f"SELECT name FROM sweeps WHERE project='{PROJECT}'",
        )
    }


def _sweep_submitted_at(name):
    """UTC epoch seconds parsed from a sweep name's trailing timestamp.

    Sweep names end in the deploy timestamp (``..._%Y%m%d-%H%M%S`` UTC)
    minted by the submitting machine, so it identifies the submission
    regardless of when its events replay onto the server.
    """
    try:
        parsed = datetime.strptime(name.rsplit("_", 1)[-1], "%Y%m%d-%H%M%S").replace(
            tzinfo=UTC
        )
    except ValueError:
        return None
    return parsed.timestamp()


def run_sweep(server, backend_name, config_file, discover_timeout=1500):
    """Submit a sweep and return its sweep name.

    The CLI does not print the sweep name. Sweep events only reach the
    server via the remote post-hook's replay (deploy-time shipping is a
    no-op for remote backends), so the new row appears in ``sweeps`` when
    the first post-hook replays — up to a partition-queue wait later. The
    name's embedded deploy timestamp (this machine's clock) identifies
    exactly this submission: a straggler sweep replaying late carries its
    own older timestamp, and the row's ``created_ns`` cannot be trusted
    (the replay races the epoch-stamped reconcile event that may create
    the row first).
    """
    prefix = f"{PROJECT}_{Path(config_file).stem}_"
    floor = time.time()
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

    def new_sweep():
        names = {
            n
            for n in studies(server)
            if n.startswith(prefix)
            and (submitted := _sweep_submitted_at(n)) is not None
            and submitted >= floor
        }
        return names.pop() if len(names) == 1 else None

    return wait_for(new_sweep, discover_timeout)


# ── assertions against the tracking server ───────────────────────────────────


def _sweep_filter(study):
    return f"sweeps.project='{PROJECT}' AND sweeps.name='{study}'"


def terminal_trials(server, study):
    states = ", ".join(f"'{s}'" for s in TERMINAL_TRIAL_STATES)
    rows = query(
        server,
        f"SELECT COUNT(*) FROM trials JOIN sweeps USING (sweep_id) "
        f"WHERE {_sweep_filter(study)} AND trials.state IN ({states})",
    )
    return rows[0][0] if rows else 0


def wait_for_trial_end(server, study, expected, timeout):
    return wait_for(lambda: terminal_trials(server, study) == expected or None, timeout)


def wait_for_settled(server, study, min_count, timeout, quiet=40):
    """Poll terminal trials until they reach min_count and stop growing.

    Retry sweeps have a non-deterministic terminal count (retried trials may
    succeed or exhaust), so we wait for quiescence rather than an exact number.
    """
    deadline = time.time() + timeout
    last = -1
    stable_at = None
    while time.time() < deadline:
        current = terminal_trials(server, study)
        if current >= min_count:
            if current != last:
                last, stable_at = current, time.time()
            elif time.time() - stable_at >= quiet:
                return current
        time.sleep(5)
    return last


def value_count(server, study):
    rows = query(
        server,
        f"SELECT COUNT(*) FROM tracked_values "
        f"JOIN executions USING (execution_id) "
        f"JOIN trials USING (trial_id) "
        f"JOIN sweeps USING (sweep_id) "
        f"WHERE {_sweep_filter(study)}",
    )
    return rows[0][0] if rows else 0


def metric_max(server, study, key):
    rows = query(
        server,
        f"SELECT MAX(scalar_val) FROM tracked_values "
        f"JOIN executions USING (execution_id) "
        f"JOIN trials USING (trial_id) "
        f"JOIN sweeps USING (sweep_id) "
        f"WHERE {_sweep_filter(study)} AND tracked_values.key='{key}'",
    )
    return rows[0][0] if rows and rows[0][0] is not None else None


def first_artifact(server, study):
    """Return (trial_id, key, artifact_id) of the sweep's first declared artifact."""
    rows = query(
        server,
        f"SELECT trial_id, key, artifact_id FROM artifacts "
        f"JOIN trials USING (trial_id) "
        f"JOIN sweeps USING (sweep_id) "
        f"WHERE {_sweep_filter(study)} LIMIT 1",
    )
    return tuple(rows[0]) if rows else None


def fetch_artifact(server, artifact_id):
    host, base_url = server
    res = ssh(
        host,
        f"curl -s -f {base_url}/artifact/{artifact_id} "
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
