# E2E Test Guide: Artifact Storage

Verifies the full artifact pipeline: manifest writes during trials → threaded upload to MinIO → tracking events streamed to gRPC server → post-hook sync of remaining data.

## What this tests

- `tracker.log_artifact(key, local_path)` writes to per-trial manifest files
- ArtifactUploader threads upload files to MinIO via boto3
- Tracking events (params, metrics, artifacts, trial_end) stream to the gRPC server
- Post-hook uploads optuna journal and syncs remaining artifacts
- Clean command guards against unsynced data

## Prerequisites

The NixOS module must be deployed and env vars set. Verify:

```bash
# Tracking server reachable (gRPC over TLS through Funnel)
echo $JERNERICS_TRACKING_SERVER
# Should be something like: atlas.<tailnet>.ts.net:443

# MinIO reachable (HTTPS through Funnel)
echo $AWS_ENDPOINT_URL
# Should be something like: https://atlas.<tailnet>.ts.net:8443

# Credentials and bucket
echo $AWS_ACCESS_KEY_ID
echo $AWS_SECRET_ACCESS_KEY
echo $JERNERICS_ARTIFACT_BUCKET
```

Quick connectivity check:

```bash
# MinIO S3 API
curl -s -o /dev/null -w "%{http_code}" $AWS_ENDPOINT_URL/minio/health/live
# Should return 200

# Tracking server — use grpcurl with proto file (no reflection)
nix-shell -p grpcurl --run "grpcurl -import-path packages/jernerics-proto/proto -proto tracking.proto $JERNERICS_TRACKING_SERVER list"
# Should return: jernerics.tracking.TrackingService
# Requires running from repo root for the proto import path
```

If any prerequisite fails, **stop and report the failure**.

---

## Test 1: Local (no backend, no container)

Tests the in-process path: manifest writes + tracking events + artifact uploads, all on the local machine.

### 1a. Run the sweep

```bash
cd examples/sweep-artifacts
jernerics local dag.py config.py
```

**Pass:** Prints "Trial 1 completed", "Trial 2 completed", "Trial 3 completed", exits 0.

**Fail:** Check stderr for traceback. Common issues:
- Missing env vars → artifact uploader silently skips, manifests still written
- gRPC connection refused → tracking server not running or wrong address

### 1b. Inspect tracking server

Query DuckDB on the tracking server host. The server holds a read-write connection, so open a second regular (read-write) connection — do not use `read_only=True` (DuckDB rejects mixed access modes).

```bash
ssh root@atlas.local "nix-shell -p duckdb --run 'duckdb /var/lib/jernerics/db.duckdb \"SELECT * FROM artifacts\"'"
```

**Verify:** 3 artifact events (one per trial), 3 trial_end events.

Alternatively, using the server's own Python:

```bash
ssh root@atlas.local "/nix/store/x3sfccix7fysnsmj3d8bh01arsa106fc-jernerics-server-env/bin/python -c \"import duckdb; con = duckdb.connect('/var/lib/jernerics/db.duckdb'); print(con.execute('SELECT * FROM artifacts').fetchall()); print(con.execute('SELECT * FROM trial_end').fetchall())\""
```

### 1c. Inspect MinIO

```bash
nix-shell -p minio-client --run "mc alias set local $AWS_ENDPOINT_URL $AWS_ACCESS_KEY_ID $AWS_SECRET_ACCESS_KEY"
nix-shell -p minio-client --run "mc ls --recursive local/$JERNERICS_ARTIFACT_BUCKET/sweep-artifacts/"
```

**Verify:** 3 files: `{study_name}/0/summary-0.txt`, `{study_name}/1/summary-1.txt`, `{study_name}/2/summary-2.txt`.

```bash
nix-shell -p minio-client --run "mc cat local/$JERNERICS_ARTIFACT_BUCKET/sweep-artifacts/{study_name}/0/summary-0.txt"
```

**Verify:** Content is "Trial 0, seed=42".

### 1d. Clean

```bash
jernerics clean --backend pueue-local
```

**Pass:** Reports no unsynced data, cleans successfully.

**Fail:** If it reports unsynced manifests, the uploader didn't finish or cursor didn't advance.

---

## Test 2: Remote Pueue + Docker (scimlab)

Tests env var passthrough through Docker containers.

### 2a. Build container (Docker image)

```bash
jernerics build --backend pueue-remote
```

**Pass:** Prints "Build job submitted: <id>".

Wait for the pueue task to complete:
```bash
ssh jez21005@scimlab.engr.uconn.edu pueue status
```

Once done, check build logs:
```bash
ssh jez21005@scimlab.engr.uconn.edu pueue log <task_id>
```

**Pass:** Shows `docker build` output ending with "Successfully tagged container.sif".

Verify the image exists:
```bash
ssh jez21005@scimlab.engr.uconn.edu docker image inspect container.sif
```

**Fail:** If `docker build` fails, check that Dockerfile was synced to the remote:
```bash
ssh jez21005@scimlab.engr.uconn.edu "ls ~/projects/jernerics-examples/sweep-artifacts/Dockerfile"
```

### 2b. Dry run

```bash
jernerics run --backend pueue-remote dag.py config.py --dry-run
```

**Verify the generated script contains:**
- `-e AWS_ENDPOINT_URL=...`
- `-e AWS_ACCESS_KEY_ID=...`
- `-e AWS_SECRET_ACCESS_KEY=...`
- `-e JERNERICS_ARTIFACT_BUCKET=...`
- `-e JERNERICS_TRACKING_SERVER=...`

### 2c. Submit sweep

```bash
jernerics run --backend pueue-remote dag.py config.py
```

**Pass:** Prints sweep submission confirmation.

### 2d. Check status and logs

```bash
ssh jez21005@scimlab.engr.uconn.edu pueue status
```

Wait for tasks to complete, then:

```bash
jernerics logs --backend pueue-remote <task_id>
```

**Pass:** Shows trial output, no connection errors.

### 2e. Inspect MinIO

```bash
nix-shell -p minio-client --run "mc ls --recursive local/$JERNERICS_ARTIFACT_BUCKET/sweep-artifacts/"
```

**Verify:** 6 files total now (trials 0-2 from local, trials 0-2 from remote).

---

## Test 3: HPC (SLURM + Apptainer)

Tests env var passthrough through Apptainer `--env` flags.

### 3a. Build container (Apptainer SIF)

```bash
jernerics build --backend hpc
```

**Pass:** Prints "Build job submitted: <id>".

Wait for completion:
```bash
jernerics logs --backend hpc <id> --follow
```

Verify the container exists:
```bash
ssh jez21005@hpc2.storrs.hpc.uconn.edu "ls ~/projects/jernerics-examples/sweep-artifacts/container.sif"
```

**Fail:** If build log shows errors, check that `container.def` was synced and `build_dir` is accessible.

### 3b. Dry run

```bash
jernerics run --backend hpc dag.py config.py --dry-run
```

**Verify the generated script contains:**
- `--env AWS_ENDPOINT_URL=...`
- `--env AWS_ACCESS_KEY_ID=...`
- `--env AWS_SECRET_ACCESS_KEY=...`
- `--env JERNERICS_ARTIFACT_BUCKET=...`
- `--env JERNERICS_TRACKING_SERVER=...`

### 3c. Submit sweep

```bash
jernerics run --backend hpc dag.py config.py
```

**Pass:** Prints job submission confirmation.

### 3d. Check jobs and logs

```bash
jernerics jobs --backend hpc
```

Wait for the job to start, then:

```bash
jernerics logs --backend hpc <id> --array-index 1
```

**Pass:** Shows trial output with no connection errors.

### 3e. Inspect MinIO

```bash
nix-shell -p minio-client --run "mc ls --recursive local/$JERNERICS_ARTIFACT_BUCKET/sweep-artifacts/"
```

**Verify:** 9 files total (trials from all three backends).

---

## Cleanup

```bash
# Clean local pueue state
pueue clean

# Clean remote pueue state
ssh jez21005@scimlab.engr.uconn.edu pueue clean

# Clean HPC (dry run first)
jernerics clean --backend hpc

# Optionally remove artifacts from MinIO
# nix-shell -p minio-client --run "mc rm --recursive local/$JERNERICS_ARTIFACT_BUCKET/sweep-artifacts/"
```

## Reporting

For each test step, report:
1. **Step identifier** (e.g., "1b", "3c")
2. **PASS/FAIL**
3. **Full output** on failure (stdout + stderr)
4. **Hypothesis** if the cause is obvious from the error

Do not attempt to fix failures. Report them and stop.
