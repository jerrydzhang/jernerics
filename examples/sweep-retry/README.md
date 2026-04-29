# sweep-retry

E2E test for the jernerics auto-retry system. One dag, three configs
exercising two failure modes (type 1 and type 2).

## Test scenarios

### config_app_crash — application crash (type 1)

Trials 1 and 4 raise RuntimeError. Optuna records FAIL state.
Checker sees incomplete trials, submits fresh ones with new params.

| Trial | lr | dropout | Result |
|-------|------|---------|--------|
| 0 | 1e-3 | 0.3 | complete |
| 1 | 1e-3 | 0.7 | **CRASH** (app) |
| 2 | 1e-2 | 0.3 | complete |
| 3 | 1e-2 | 0.7 | complete |
| 4 | 1e-1 | 0.3 | **CRASH** (app) |
| 5 | 1e-1 | 0.7 | complete |

```
Array 1 (6 tasks): 4 complete, 2 crash.
Checker 1: 2 fresh trials needed. Submits array of 2.
Array 2 (2 tasks): New trial numbers. Both succeed.
Checker 2: All done. Chain ends.
```

### config_node_death — simulated node death, single retry (type 2)

Trials 1 and 4 call `os._exit(9)`, killing the process immediately.
No exception handling runs — Optuna trial stays RUNNING, heartbeat
thread dies (file goes stale). Checker detects stale RUNNING trials,
marks them FAIL, enqueues same params, writes ledger.

| Trial | lr | dropout | Result |
|-------|------|---------|--------|
| 0 | 1e-3 | 0.3 | complete |
| 1 | 1e-3 | 0.7 | **KILL** (os._exit) |
| 2 | 1e-2 | 0.3 | complete |
| 3 | 1e-2 | 0.7 | complete |
| 4 | 1e-1 | 0.3 | **KILL** (os._exit) |
| 5 | 1e-1 | 0.7 | complete |

```
Array 1 (6 tasks): 4 complete, 2 killed.
Checker 1: Detects 2 stale RUNNING trials. Marks FAIL. Enqueues same params.
           Ledger: {param_key: 1, param_key: 1}. Submits array of 2 (retries).
Array 2 (2 tasks): New trial numbers. Both succeed.
Checker 2: All done. Chain ends.
```

### config_node_death_persistent — node death with retry exhaustion (type 2)

Single trial with lr=1e-4 (below lr_fatal=5e-4). Every retry gets the
same params via `enqueue_trial`, so it dies again. After max_retries (3),
the checker gives up on those params and submits a fresh trial.

```
Round 1: lr=1e-4, crash.       Ledger: {key: 1}
Round 2: lr=1e-4 (enqueued), crash.  Ledger: {key: 2}
Round 3: lr=1e-4 (enqueued), crash.  Ledger: {key: 3}
Round 4: lr=1e-4 (enqueued), crash.  Count=3 >= max_retries. Exhausted.
         Fresh trial submitted.
Round 5: lr=1e-3 (fresh). Succeeds. Chain ends.
```

Verify:
- 5 array jobs + 5 checker jobs
- Ledger: `{key: 3}` (param combo retried 3 times then abandoned)
- 1 COMPLETE + 4 FAIL in Optuna study

## How crashes are controlled

- **App crash** (`crash_app_on`): raises RuntimeError. Optuna records FAIL.
- **Node death by trial number** (`crash_node_on`): calls `os._exit(9)` on
  specific trial numbers. Retries get new numbers → survive.
- **Node death by param value** (`lr_fatal`): calls `os._exit(9)` when
  lr < lr_fatal. Retries get same params via enqueue → also die.
- **Happy path**: trials not matching any crash condition complete normally.

## Retry configuration

Fast intervals for testing (not production values):

| Parameter | Value | Notes |
|-----------|-------|-------|
| `heartbeat_interval_s` | 10 | Touch heartbeat every 10s |
| `stale_after_s` | 20 | No heartbeat for 20s → stale |
| `grace_period_s` | 20 | Checker waits 20s before checking |
| `max_retries` | 3 | Each param combo retried up to 3 times |
| `chain_depth_cap` | 10 | Safety limit on chain length |

## Failure mode: pre-start failure (type 3)

No config simulates pre-start failures (auth error, node corruption
before container starts). No Optuna trial is created. The checker sees
`completed + running + waiting < n_trials` and submits fresh trials.
Tested by unit tests of `plan_retry`. Cannot be simulated in e2e without
patching script generation.

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

### Step 2: Test app crash (type 1)

```bash
jernerics run --backend hpc dag.py config_app_crash.py
```

Verify: 2 array jobs + 2 checker jobs. Ledger: `{}`.

### Step 3: Test node death, single retry (type 2)

```bash
jernerics run --backend hpc dag.py config_node_death.py
```

Verify: 2 array jobs + 2 checker jobs. Ledger: `{key: 1, key: 1}`.

### Step 4: Test node death with retry exhaustion (type 2)

```bash
jernerics run --backend hpc dag.py config_node_death_persistent.py
```

Verify: 5 array jobs + 5 checker jobs. Ledger: `{key: 3}`. Study has 4 FAIL + 1 COMPLETE.

## What NOT to do

- Do NOT run `uv sync` — this project uses uv2nix
- Do NOT set `stale_after_s` below 10s (must be ≥ 2× `heartbeat_interval_s`)
- Do NOT set `grace_period_s` below `stale_after_s`
- Do NOT run `clean --force` between array and checker
