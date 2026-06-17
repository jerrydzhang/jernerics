---
base_branch: main
branch: null
completed_at: '2026-06-17T03:34:43.237041'
created: '2026-06-17T02:28:06.213959'
merge_commit_sha: 08dfad263bc823ccfc96b6229989e7c4b776f726
parent_id: null
retry_count: 0
status: closed
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
