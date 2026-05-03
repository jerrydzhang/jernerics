# Retry System

## Overview

Jernerics detects node deaths (hard crashes like `os._exit()`,
OOM kills) via heartbeat staleness and automatically resubmits
affected trials.

## How it works

1. During trial execution, a heartbeat file is periodically updated
   in the tracking directory.
2. After the sweep array job completes, the post-hook pipeline runs
   a retry checker.
3. The checker compares heartbeat timestamps against a staleness
   threshold.
4. Stale trials are identified and resubmitted as a new array job.
5. If trials exhaust their max retry count, they are marked as
   permanently failed.

## Retry context

A `RetryContext` JSON file is written alongside the sweep submission.
It contains everything the checker needs: study name, backend config,
file paths, chain depth (to prevent infinite retry loops).

## Configuration

Retry behavior is controlled per-config via the config file:

- **Staleness threshold** — how long (seconds) without a heartbeat
  before a trial is considered dead.
- **Max retries** — how many times a trial can be resubmitted before
  giving up.

## Failure modes

| Failure type | Detection | Behavior |
|-------------|-----------|----------|
| App exception | Trial exits non-zero | Trial marked FAILED, no retry |
| Node death (`os._exit`) | Heartbeat stale | Trial retried up to max_retries |
| Persistent failure | Same params always die | Exhausts retries, marked failed |

App-level exceptions (Python exceptions, `RuntimeError`) are normal
Optuna failures — the trial is marked as FAIL and the sweep continues.
They do NOT trigger retry.

Only hard crashes (where the process disappears without cleanup)
trigger the heartbeat-based retry path.

## Post-hook pipeline

After a sweep completes, the post-hook runs:

1. **Retry check** — detect stale heartbeats, submit retry job if needed
2. **Tracking replay** — sync local tracking data to the gRPC server
3. **Artifact sync** — upload artifacts to S3 storage

If a retry job is submitted, the post-hook returns early. The retry
job will run its own post-hook when it completes.
