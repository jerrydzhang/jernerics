---
base_branch: main
branch: null
created: '2026-06-17T04:25:42.513009'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: failed
title: Add human timestamp formatting option to observability tables
---

# Plan

Add human timestamp formatting option to observability tables

Many observability tables print raw timestamp_ns values, which are exact but hard to read. Add an opt-in human-readable timestamp display without changing JSON output.

Files:
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add a small private helper that converts timestamp_ns to UTC ISO-8601 string.
- Add --human-time option to sweeps, metric-history, artifacts, results, and params commands.
- When --human-time is set, human table timestamp columns should show the ISO string instead of raw ns.
- JSON output must remain unchanged.
- If timestamp_ns is missing or null, keep the cell blank.
- Do not change server responses.

Validation:
- .abraxas/validate.sh
