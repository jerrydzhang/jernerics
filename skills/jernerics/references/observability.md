# Observability

Post-hoc analysis of tracked runs: list them, drill into one, or compare
two. These commands read the tracking server over HTTP — no backend or
job required, so they work from your laptop against a remote server.

## Commands

All three connect via `JERNERICS_TRACKING_SERVER` (or
`[tool.jernerics] tracking_server`) and accept `--json` for machine
consumption: identical data, raw values, no human formatting (large
numbers stay full-precision, slopes as raw floats).

```bash
jernerics runs                        # all runs in this project
jernerics runs --json                 # same, as a JSON array

jernerics summary <run>               # one run's full analysis
jernerics summary <run> --json

jernerics diff <run_a> <run_b>        # compare two runs
jernerics diff <run_a> <run_b> --json
```

A **run id** is `study_name` (trial 0) or `study_name:trial_id`, e.g.
`symlab-131` or `sweep_42:3`.

## `jernerics runs`

One row per `(study_name, trial_id)`: status (completed/running), step
range, the **priority metric's** final value, duration, created time,
and all params. The priority column is the first of `loss`, `error`,
`accuracy`, `r2` that the run logs; if none apply, the column is omitted.

## `jernerics summary <run>`

Params, artifacts, and a per-metric table. Each metric row reports:

- **First / Last / Change** — earliest and latest logged values
  (chronological by log order), and `last - first`.
- **Slope [a-b]** — least-squares slope (value per step) over the first
  10% and last 10% of points. The `[a-b]` header is the step span each
  window covers.
- **n_points** — total scalar points for the metric.

Slopes are skipped (shown as `-`) when a 10% window would hold fewer
than 5 points — i.e. series shorter than ~50 logged points get no slope.
All metrics are treated identically and sorted alphabetically.

### Reading the slopes

After pulling a summary, compare the early and recent slopes of each
metric. If `|early| / |recent|` (or the inverse) exceeds roughly **10×**,
the metric's behaviour changed substantially during training — it is
not a smooth trajectory. Common causes: a warmup ramp ending, a
learning-rate schedule step, divergence/recovery, or a regime change.
Before drawing conclusions from the summary alone, fetch the raw series
and look at the actual curve:

```sql
-- via /query, or any sqlite client against the store
SELECT step, scalar_val
FROM tracked_values
WHERE project = ? AND study_name = ? AND trial_id = ?
  AND key = ? AND value_type = 'scalar' AND scalar_val IS NOT NULL
ORDER BY step;
```

A near-zero recent slope with a healthy change usually means the metric
has settled; a recent slope still comparable to the early one means it
is still moving.

## `jernerics diff <run_a> <run_b>`

Lists params that differ (with each run's value) and params that match
(count + keys), then a metric table of each run's **final** value with
the change (`b - a`). Diff compares final values only — not trajectory
shape. A metric present in one run but not the other shows a blank side
and no change.

## When to use the commands vs raw SQL

- **`runs` / `summary` / `diff`** — quick lookups and comparisons. Fast,
  formatted, opinionated about what matters (priority metric, slopes,
  final values). The right first move for "how did this go?" or "how do
  these two differ?".
- **Raw SQL via `/query`** — anything the summaries do not cover:
  custom aggregations, joins across studies, per-step series, filtering
  by `context` JSON, histograms, correlation between params and metric
  outcomes across many trials. `/query` accepts read-only `SELECT`/
  `WITH`/`VALUES` SQL plus optional bound `params` (a JSON array), and
  caps results at 10 000 rows.

Prefer the summary for orientation, then drop to SQL once you know which
series or cross-cut you need.
