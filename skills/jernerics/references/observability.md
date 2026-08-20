# Observability

Post-hoc analysis of tracked trials: list them, drill into one, compare
two, or pull a raw series. These commands read the tracking server's
typed domain endpoints over HTTP — no backend or job required, so they
work from your laptop against a remote server.

## Commands

All connect via `JERNERICS_TRACKING_SERVER` (or
`[tool.jernerics] tracking_server`) plus the project name from
`pyproject.toml`, and accept `--json` for machine consumption:

```bash
jernerics tracking runs                        # every trial in this project
jernerics tracking runs --json

jernerics tracking summary <ref>               # one trial, everything stored
jernerics tracking summary <ref> --json

jernerics tracking diff <ref_a> <ref_b>        # compare two trials
jernerics tracking trace <ref> <key>           # one value key's step series
jernerics tracking query "<sql>"               # raw read-only SQL escape hatch
```

A **trial ref** is `<sweep-name>:<trial-number>` (e.g. `sweep:3`) or a
raw 32-hex trial id.

## `jernerics tracking runs`

One row per trial: sweep, number, state, objective, retry root, the
derived monitoring label (active / quiet / stale / ended / unknown,
folded from the trial's executions), param and value counts, and last
activity.

## `jernerics tracking summary <ref>`

One trial's full record: lineage (the whole retry family ordered by
generation), params split by kind (sampled vs manual), the value
catalog (per key: type, point count, latest step), artifact
declarations (repeated keys read as versions), and every execution
with its outcome.

## `jernerics tracking diff <ref_a> <ref_b>`

Params union (each side's value, blank when absent), latest values per
key on both sides, and both objectives.

## `jernerics tracking trace <ref> <key>`

The `[step, value]` series for one value key on one trial — scalar
values as floats, JSON observations as canonical JSON text. Unsummarized;
use `--json` to reason over the full series.

## When to use the commands vs raw SQL

- **runs / summary / diff / trace** — quick lookups and comparisons
  through typed records. The right first move for "how did this go?"
  or "how do these two differ?".
- **`tracking query`** — the expert escape hatch for anything the typed
  surface cannot answer: custom aggregations, joins across sweeps,
  correlations. Read-only statements only, capped at 10 000 rows and a
  runtime budget. Prefer the summary for orientation, then drop to SQL
  once you know which series or cross-cut you need.

The same data is available programmatically via
`jernerics.tracking.TrackingClient` (typed records, no SQL) — see
`references/tracking.md`.
