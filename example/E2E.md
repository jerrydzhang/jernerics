# E2E Test Guide

Automated verification of the full jernerics pipeline across three
backend+container combinations, exercising DAG execution, Optuna sweeps,
tracking streams, artifact storage, GPU detection, and retry logic.

Run all commands from `example/`.

## Three backends

| Backend | Container | Host | What it tests |
|---------|-----------|------|---------------|
| `hpc` | Apptainer | `hpc2.storrs.hpc.uconn.edu` | Slurm array jobs + Apptainer build staging (`build_dir`) |
| `pueue-remote` | Docker | `scimlab.engr.uconn.edu` | Pueue on remote + Docker build + env var passthrough |
| `pueue-local` | None | local | Pueue on localhost + no container passthrough |

## Prerequisites

### SSH access

```bash
ssh jez21005@hpc2.storrs.hpc.uconn.edu echo ok
ssh jez21005@scimlab.engr.uconn.edu echo ok
```

### Local pueue daemon

```bash
pueue status
```

### Remote pueue daemon

```bash
ssh jez21005@scimlab.engr.uconn.edu pueue status
```

### Docker on scimlab

```bash
ssh jez21005@scimlab.engr.uconn.edu docker ps
```

### Tracking server + MinIO (for artifact/tracking tests)

```bash
# Env vars must be set in the current shell
echo $JERNERICS_TRACKING_SERVER
echo $JERNERICS_API_KEY
echo $AWS_ENDPOINT_URL
echo $AWS_ACCESS_KEY_ID
echo $AWS_SECRET_ACCESS_KEY
echo $JERNERICS_ARTIFACT_BUCKET

# Connectivity checks
curl -sk -o /dev/null -w "%{http_code}" $AWS_ENDPOINT_URL/minio/health/live
# → 200
curl -sk -o /dev/null -w "%{http_code}" -X POST http://atlas.local:8081/query \
  -H "Authorization: Bearer $JERNERICS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT 1"}'
# → 200
```

If any prerequisite fails, **stop and report the failure**.

---

## Test 1: Local sweep (no backend, no container)

Tests the in-process path end-to-end: DAG execution → Optuna → tracking
stream → artifact manifests → post-hook pipeline (replay + sync).

### 1a. Run basic sweep

```bash
jernerics local dag.py config.py
```

**Pass:** Prints 5 "Running trial N/5" lines, exits 0.

### 1b. Verify artifacts were logged

```bash
ls artifacts-out/
```

**Pass:** Files `summary-0.txt` through `summary-4.txt` exist, each
containing trial results.

### 1c. Verify tracking data streamed to server

```bash
curl -s -X POST http://atlas.local:8081/query \
  -H "Authorization: Bearer $JERNERICS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT COUNT(*) FROM trial_end"}'
```

**Pass:** Returns JSON with `"rows": [[N]]` where N > 0 (cumulative across all tests).

### 1d. Verify artifacts uploaded to MinIO

```bash
mc ls --recursive local/jernerics/sweep-e2e/
```

**Pass:** Files under `*/N/summary-N.txt` present.

### 1e. Clean up local artifacts dir

```bash
rm -rf artifacts-out
```

---

## Test 2: Remote Pueue + Docker (scimlab)

Tests env var passthrough through Docker containers (`-e` flags),
artifact upload from inside containers, and post-hook execution.

### 2a. Build container

```bash
jernerics build --backend pueue-remote
```

**Pass:** Prints "Build job submitted: <id>".

Wait for completion:
```bash
ssh jez21005@scimlab.engr.uconn.edu pueue status
```

Once done, verify:
```bash
ssh jez21005@scimlab.engr.uconn.edu docker image inspect sweep-e2e
```

**Pass:** Image exists. Note: the image name is `sweep-e2e` (the project
name), not `container.sif`.

### 2b. Dry run — verify env var passthrough

```bash
jernerics run --backend pueue-remote dag.py config.py --dry-run
```

**Verify the generated script contains:**
- `-e AWS_ENDPOINT_URL=...`
- `-e AWS_ACCESS_KEY_ID=...`
- `-e AWS_SECRET_ACCESS_KEY=...`
- `-e JERNERICS_ARTIFACT_BUCKET=...`

These env vars must appear in **both** the trial command wrap and the
post-hook command wrap.

### 2c. Submit sweep

```bash
jernerics run --backend pueue-remote dag.py config.py
```

**Pass:** Prints sweep submission confirmation with task group name.

### 2d. Check status and wait for completion

```bash
ssh jez21005@scimlab.engr.uconn.edu pueue status
```

Wait until all tasks show "Done", then check logs:
```bash
jernerics logs --backend pueue-remote <task_id>
```

**Pass:** Shows 5 trial outputs with DAG execution (generate → train →
evaluate). No gRPC connection errors.

### 2e. Verify tracking data on server

```bash
curl -s -X POST http://atlas.local:8081/query \
  -H "Authorization: Bearer $JERNERICS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT COUNT(*) FROM params"}'
```

**Pass:** Returns JSON with count increased from test 1 (cumulative).

### 2f. Verify artifacts in MinIO

```bash
mc ls --recursive local/jernerics/sweep-e2e/
```

**Pass:** Additional files from this test's trials (new study prefix).

---

## Test 3: HPC (Slurm + Apptainer)

Tests Slurm array jobs, Apptainer `--env` passthrough, and build
staging on `/dev/shm`.

### 3a. Build container (staged on /dev/shm)

```bash
jernerics build --backend hpc
```

**Pass:** Prints "Build job submitted: <id>".

Wait for completion:
```bash
jernerics logs --backend hpc <id> --follow
```

**Verify the build used staged directory** — the build log should
contain:
- `mkdir -p /dev/shm/build/sweep-e2e`
- `APPTAINER_TMPDIR=/dev/shm/build/sweep-e2e`
- `rm -rf /dev/shm/build/sweep-e2d`

Verify the container exists:
```bash
ssh jez21005@hpc2.storrs.hpc.uconn.edu "ls ~/projects/jernerics-examples/sweep-e2e/container.sif"
```

### 3b. Dry run — verify env var passthrough

```bash
jernerics run --backend hpc dag.py config.py --dry-run
```

**Verify the generated script contains:**
- `--env AWS_ENDPOINT_URL=...`
- `--env AWS_ACCESS_KEY_ID=...`
- `--env AWS_SECRET_ACCESS_KEY=...`
- `--env JERNERICS_ARTIFACT_BUCKET=...`

These must appear in **both** the trial `apptainer exec` and the
post-hook `apptainer exec`.

Also verify the script contains:
- `#SBATCH --array=1-5%10`
- Post-hook dependency line (`--dependency=afterany:<array_id>`)

### 3c. Submit sweep

```bash
jernerics run --backend hpc dag.py config.py
```

**Pass:** Prints syncing, then "Job submitted: <id>". Record the job ID.

### 3d. Monitor and check logs

```bash
jernerics jobs --backend hpc
```

Wait for the job to start, then:
```bash
jernerics logs --backend hpc <id> --follow
```

**Pass:** Shows Slurm array task output with trial execution.

### 3e. Verify post-hook ran

After the array job completes, check that a post-hook job ran:
```bash
jernerics jobs --backend hpc --all
```

**Pass:** Two jobs listed — the array job and the post-hook job. The
post-hook should show COMPLETED.

### 3f. Verify artifacts in MinIO

```bash
mc ls --recursive local/jernerics/sweep-e2e/
```

**Pass:** Additional files from this test's HPC trials.

---

## Test 4: Local Pueue (no container)

Tests the passthrough path with no container isolation.

### 4a. Submit sweep

```bash
jernerics run --backend pueue-local dag.py config.py
```

**Pass:** Prints "Sweep submitted: group <name>".

### 4b. Wait and verify

```bash
pueue status
```

Wait for all tasks to complete, then:
```bash
jernerics logs --backend pueue-local <task_id>
```

**Pass:** Shows trial output with DAG execution.

### 4c. Clean up pueue state

```bash
pueue clean
```

---

## Test 5: GPU detection

Tests the `detect_gpu` task on HPC with GPU partition. Uses
`config_gpu.py` — 1 trial, `priority-gpu` partition, `gres: gpu:1`.
On pueue-remote the workstation always has GPU access.

### 5a. Local GPU check

```bash
jernerics local dag.py config_gpu.py
```

**Pass:** Exits 0. Trial output includes `detect_gpu` result showing
`cuda_available` (true or false depending on local hardware). No crash
regardless of CUDA availability.

### 5b. HPC with GPU

```bash
jernerics run --backend hpc dag.py config_gpu.py
```

**Pass:** Job submitted to `priority-gpu` partition. Trial output
should show `cuda_available: True` and a GPU device name.

### 5c. pueue-remote (always has GPU)

```bash
jernerics run --backend pueue-remote dag.py config_gpu.py
```

**Pass:** Trial output shows `cuda_available: True`.

---

## Test 6: Retry — app crash

Tests app-level failure handling with `config_retry_app.py`.
6-trial grid sweep; trials 1 and 4 raise `RuntimeError`.
Failed trials should be reported in the sweep summary; successful
trials complete normally.

### 6a. Run locally

```bash
jernerics local dag.py config_retry_app.py
```

**Pass:** Exits 0. Trials 1 and 4 show RuntimeError in output.
Other 4 trials complete normally with loss values.

### 6b. Run on HPC

```bash
jernerics run --backend hpc dag.py config_retry_app.py
```

**Pass:** Array job with 6 tasks. Failed trials visible in logs.
Post-hook runs after array completes.

---

## Test 7: Retry — node death

Tests heartbeat staleness detection with `config_retry_node.py`.
6-trial grid sweep; trials 1 and 4 call `os._exit(9)`, simulating
node death. Requires retry infrastructure (heartbeat + checker) to
resubmit dead trials.

### 7a. Run on HPC

```bash
jernerics run --backend hpc dag.py config_retry_node.py
```

**Pass:** Array job submitted. Dead trials (1, 4) are detected by
heartbeat checker and resubmitted. Final sweep summary includes all
6 trials completed or retried.

---

## Test 8: Retry — persistent failure

Tests max-retries exhaustion with `config_retry_persistent.py`.
2 trials; any trial with `lr < 5e-4` (i.e., `lr=1e-4`) dies via
`os._exit(9)` every time. Retried trials get the same params and
die again until max_retries is exhausted.

### 8a. Run locally

```bash
jernerics local dag.py config_retry_persistent.py
```

**Pass:** Trial with `lr=1e-4` exhausts retries and is marked failed.
Trial with `lr=1e-3` completes normally.

---

## Test 9: Clean guards unsynced data

Verifies the clean command refuses when unsynced data exists.

### 5a. Create unsynced tracking data

Manually create a stale .pb file in the tracking dir (using the most
recent study name from test 1):

```bash
# Find the most recent study
ls ~/.cache/jernerics/sweep-e2e/tracking/

# Create a fake .pb file in its events dir
STUDY=$(ls -t ~/.cache/jernerics/sweep-e2e/tracking/ | head -1)
touch ~/.cache/jernerics/sweep-e2e/tracking/$STUDY/events/99.pb
```

### 5b. Verify clean refuses

```bash
jernerics clean --backend pueue-local --force
```

**Pass:** Prints "Error: Unsynced tracking data found. Run sync first."
and exits non-zero.

### 5c. Clean up the fake file

```bash
rm ~/.cache/jernerics/sweep-e2e/tracking/$STUDY/events/99.pb
```

---

## Test 10: Sync command

Verifies the sync command replays tracking data from remote to server.

### 6a. Run a sweep on pueue-remote (if not already done in test 2)

Skip if test 2 was already run and completed successfully.

### 6b. Run sync

```bash
jernerics sync --backend pueue-remote --study <study_name>
```

Use the study name from test 2 (printed in the submission output).

**Pass:** Prints "Syncing tracking data from scimlab..." then "Sync
complete." No errors.

### 6c. Verify tracking data after sync

```bash
curl -s -X POST http://atlas.local:8081/query \
  -H "Authorization: Bearer $JERNERICS_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"sql": "SELECT COUNT(*) FROM params"}'
```

**Pass:** Count has increased.

---

## Cleanup

After all tests pass:

```bash
# Clean pueue-local (if not already done in test 4c)
pueue clean

# Clean remote pueue state
ssh jez21005@scimlab.engr.uconn.edu pueue clean

# Clean HPC — dry run first, then force if everything passed
jernerics clean --backend hpc
jernerics clean --backend hpc --force

# Clean remote pueue
jernerics clean --backend pueue-remote
jernerics clean --backend pueue-remote --force
```

---

## Reporting

For each test step, report:
1. **Step identifier** (e.g., "2b", "3e")
2. **PASS / FAIL**
3. **Full output** on failure (stdout + stderr)
4. **Hypothesis** if the cause is obvious from the error

Do not attempt to fix failures. Report them and stop.
