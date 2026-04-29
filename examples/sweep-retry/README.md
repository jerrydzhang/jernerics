# sweep-retry

E2E test for the jernerics auto-retry system. One dag, two configs
exercising three failure modes.

## Test scenarios

### config_transient.py — transient failures + happy path

All lr values are safe (≥ 1e-3). Trials 1 and 4 crash by trial number.
Retries get new trial numbers → succeed.

| Trial | lr | dropout | Result |
|-------|------|---------|--------|
| 0 | 1e-3 | 0.3 | complete |
| 1 | 1e-3 | 0.7 | **CRASH** |
| 2 | 1e-2 | 0.3 | complete |
| 3 | 1e-2 | 0.7 | complete |
| 4 | 1e-1 | 0.3 | **CRASH** |
| 5 | 1e-1 | 0.7 | complete |

```
Array 1 (6 tasks): 4 complete, 2 crash (trials 1,4).
Checker 1: Detects stale. Marks FAIL. Enqueues retries.
Array 2 (2 tasks): New trial numbers. Both succeed.
Checker 2: All done. Chain ends.
```

### config_permanent.py — permanent failures + happy path

lr=1e-4 is in the bad region. No transient crashes. Same params on
retry → same crash. After max_retries (3), trials 0 and 1 are abandoned.

| Trial | lr | dropout | Result |
|-------|------|---------|--------|
| 0 | 1e-4 | 0.3 | **CRASH (permanent)** |
| 1 | 1e-4 | 0.7 | **CRASH (permanent)** |
| 2 | 1e-3 | 0.3 | complete |
| 3 | 1e-3 | 0.7 | complete |
| 4 | 1e-2 | 0.3 | complete |
| 5 | 1e-2 | 0.7 | complete |

```
Array 1 (6 tasks): 4 complete, 2 crash (trials 0,1 — bad lr).
Checker 1: Retries trials 0,1. Ledger: {0:1, 1:1}.
Array 2 (2 tasks): Same lr=1e-4. Crash again.
Checker 2: Retries again. Ledger: {0:2, 1:2}.
Array 3 (2 tasks): Same crash.
Checker 3: Retries again. Ledger: {0:3, 1:3}.
Array 4 (2 tasks): Same crash.
Checker 4: Exhausted (count ≥ max_retries). No retry for 0,1.
           remaining_needed = 6 - 4 = 2 fresh trials.
           Submits 2 fresh (new params from sampler).
Array 5 (2 tasks): Fresh params. Both complete.
Checker 5: All done. Chain ends.
```

## How crashes are controlled

Everything is driven from the config. One dag handles both modes:

- **Transient**: `base["crash_on_trials"] = [1, 4]` — crash specific
  trial numbers. Retries get new numbers → succeed.
- **Permanent**: grid includes lr values in the bad region (< 5e-4).
  Same params on retry → same crash.
- **Happy path**: trials not in either crash set complete normally.
  Implicitly tested by the non-crashing trials in each config.

## Retry configuration

Fast intervals for testing (not production values):

| Parameter | Value | Notes |
|-----------|-------|-------|
| `heartbeat_interval_s` | 10 | Touch heartbeat every 10s |
| `stale_after_s` | 20 | No heartbeat for 20s → stale |
| `grace_period_s` | 20 | Checker waits 20s before checking |
| `max_retries` | 3 | Each trial retried up to 3 times |
| `chain_depth_cap` | 10 | Safety limit on chain length |

## Failure mode: task never started

Neither config simulates SLURM-level failures (task never starts).
Those happen when the scheduler can't launch the task (auth error, node
corruption). No Optuna trial is created. The checker sees
`completed + running + waiting < n_trials` and submits fresh trials.
Tested naturally on clusters with real scheduling issues.

## Prerequisites

Same as `sweep-parallel`. SSH access to the HPC and a built container.
Do **not** run `uv sync` — this project uses uv2nix.

## Integration test procedure

### Step 1: Build container

```bash
jernerics build --backend hpc
```

Wait for build:
```bash
jernerics logs --backend hpc <build_id> --follow
```

### Step 2: Test transient failures

```bash
jernerics run --backend hpc dag.py config_transient.py
```

Should print `Array: <id>, Checker: <id>`. Wait for completion.

Verify:
```bash
jernerics jobs --backend hpc --all
```

Should show 2 array jobs + 2 checker jobs (chain terminated after retry succeeded).

Ledger: `{"1": 1, "4": 1}` — each crashed trial retried once.

### Step 3: Test permanent failures

```bash
jernerics run --backend hpc dag.py config_permanent.py
```

Should print `Array: <id>, Checker: <id>`. Wait for completion.

Verify:
```bash
jernerics jobs --backend hpc --all
```

Should show 5 array jobs + 5 checker jobs (3 retries exhausted, then 2 fresh).

Ledger: `{"0": 3, "1": 3}` — bad-lr trials retried 3 times then abandoned.

## What NOT to do

- Do NOT run `uv sync` — this project uses uv2nix
- Do NOT set `stale_after_s` below 10s (must be ≥ 2× `heartbeat_interval_s`)
- Do NOT set `grace_period_s` below `stale_after_s`
- Do NOT run `clean --force` between array and checker
