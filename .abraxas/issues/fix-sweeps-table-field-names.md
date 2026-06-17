---
base_branch: null
branch: null
created: '2026-06-17T01:14:48.612302'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: Fix sweeps table field names
---

# Plan

Fix sweeps table field names

The `jernerics sweeps` human table currently reads keys named `trials`, `completed`, and `last_event`, but GET /api/sweeps returns `trial_count`, `completed_count`, and `last_event_timestamp_ns`. As a result, human output shows zero/blank values even when JSON output is correct.

Files:
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Update the sweeps human table to use `trial_count`, `completed_count`, and `last_event_timestamp_ns`.
- Keep --json output unchanged.
- Add or update a CLI test that would fail if the old field names were used.
- Do not change the server response shape.

Validation:
- .abraxas/validate.sh
