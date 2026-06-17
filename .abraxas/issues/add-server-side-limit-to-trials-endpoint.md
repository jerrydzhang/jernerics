---
base_branch: null
branch: null
created: '2026-06-17T02:28:06.213959'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: Add server-side limit to trials endpoint
---

# Plan

Add server-side limit to trials endpoint

The trials CLI currently applies --limit after fetching all trials. Add a typed server-side limit to avoid returning large payloads unnecessarily.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/tracking/test_http_api.py

Behavior:
- Add optional query parameter `limit` to GET /api/trials.
- Default server behavior should remain unlimited if limit is omitted.
- If limit is provided, return at most that many trials after sorting by trial_id.
- Reject negative limits with HTTP 400.
- Update list_trials to send limit to the server when limit is not None.
- Preserve existing CLI --limit behavior.

Validation:
- .abraxas/validate.sh
