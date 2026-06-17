---
base_branch: main
branch: null
completed_at: '2026-06-17T01:31:56.876765'
created: '2026-06-17T01:14:50.663997'
merge_commit_sha: e152ef42bb543a66b798572dbe0b3d5e2ccddf8c
parent_id: null
retry_count: 0
status: closed
title: Add typed metric history endpoint
---

# Plan

Add typed metric history endpoint

Expose time-series metric observations through a typed endpoint so clients do not need raw SQL for charts.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py

Behavior:
- Add GET /api/metrics?project=<project>&study_name=<study_name>&key=<metric_key>.
- Return JSON list sorted by trial_id, then step with objects containing:
  - trial_id
  - key
  - value
  - step, using null when the stored step is NULL
  - timestamp_ns
- Include both stepped and final metrics for the requested key.
- Return an empty list if no matching metrics exist.
- Respect bearer auth.
- Do not expose SQL to the client.

Validation:
- .abraxas/validate.sh
