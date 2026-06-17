---
base_branch: main
branch: null
completed_at: '2026-06-16T23:02:14.322631'
created: '2026-06-16T22:31:01.926317'
merge_commit_sha: 3df8af47833c83ea0b476ee30788e49d54c5dded
order: 1
parent_id: null
retry_count: 0
status: closed
title: Add typed sweeps endpoint to tracking server
---

# Plan

Add typed sweeps endpoint to tracking server

Add a typed HTTP endpoint for listing tracked sweeps without exposing raw SQL.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/tests/test_http.py

Behavior:
- Add GET /api/sweeps.
- Return JSON list of objects with:
  - project
  - study_name
  - trial_count
  - completed_count
  - last_event_timestamp_ns
- trial_count should count distinct trial ids seen in params, metrics, results, artifacts, sweep_meta, or trial_end.
- completed_count should count distinct trial ids in trial_end.
- last_event_timestamp_ns should be the max timestamp_ns across tracked event tables.
- Respect the same bearer auth dependency as /query.
- Do not remove or change /query.
- Do not expose SQL to the client.

Validation:
- .abraxas/validate.sh
