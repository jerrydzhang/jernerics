---
base_branch: main
branch: null
completed_at: '2026-06-17T02:46:01.698992'
created: '2026-06-17T02:28:00.014397'
merge_commit_sha: dc071e625ba12720ca7fdd55d702285051c41de1
parent_id: null
retry_count: 0
status: closed
title: Fix metric-history client endpoint path
---

# Plan

Fix metric-history client endpoint path

The metric history server endpoint is GET /api/metrics, but the HTTP client calls /api/metrics/history. This makes the new `jernerics metric-history` command fail against the implemented server.

Files:
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py if needed

Behavior:
- Update get_metric_history to call /api/metrics.
- Add or update a test that asserts the requested URL path is /api/metrics with project, study_name, and key query parameters.
- Do not change the server endpoint shape.

Validation:
- .abraxas/validate.sh
