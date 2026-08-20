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

Retried trials carry lineage attrs (`retry_of`, `retry_root`,
`retry_index`), so a family reads as generations in the client, CLI,
and dashboard.

## Retry context

A `RetryContext` JSON file is written alongside the sweep submission.
It contains everything the checker needs: study name, backend config,
file paths, chain depth (to prevent infinite retry loops).

## Configuration

Retry behavior is tuned on the **backend profile** in `pyproject.toml`
(`SharedConfig` fields, under `[tool.jernerics.backends.<name>]`):

```toml
[tool.jernerics.backends.hpc]
heartbeat_interval_s = 60     # how often the heartbeat file is touched
stale_after_s = 120           # seconds without a heartbeat => trial is dead
grace_period_s = 120          # checker waits this long before judging staleness
max_retries = 3               # per-param retry cap before a trial is exhausted
chain_depth_cap = 20          # max retry-job chain depth (infinite-loop guard)
fast_fail_threshold_s = 30    # a death this fast after start counts as a fast fail
max_fast_failures = 3         # fast-fail circuit-breaker threshold
```

- **`stale_after_s`** — a `RUNNING` trial whose heartbeat is older than this
  (seconds) is presumed dead (node crash, OOM) and queued for retry.
- **`max_retries`** — per-parameter-combination limit. A param set that dies
  this many times is marked exhausted (FAIL, no further retry).
- **Fast-fail breaker** (`fast_fail_threshold_s` / `max_fast_failures`) — a
  trial that dies within seconds of starting usually has a permanent cause
  (missing input, bad config, import crash), not a transient node failure. It
  is **not** retried with the same params — Optuna draws a fresh sample
  instead. Once `max_fast_failures` such deaths accumulate globally, the
  breaker trips and fast failures become terminal so a broken sweep winds down
  rather than churning. The counter resets on a healthy round with completions.
- **`chain_depth_cap`** — hard stop on retry-of-retry depth.

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
2. **Reconciliation** — snapshot every optimizer-journal trial as a
   terminal trial snapshot; conflicts with already-terminal server
   state abort without overwriting anything
3. **Tracking replay** — replay local JSONL events to the HTTP tracking server
4. **Artifact sync** — upload pending artifact blobs to the server's disk

If a retry job is submitted, the post-hook returns early. The retry
job will run its own post-hook when it completes.
