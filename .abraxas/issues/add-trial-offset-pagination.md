---
base_branch: main
branch: abraxas/add-trial-offset-pagination
completed_at: '2026-06-17T06:36:28.861269'
created: '2026-06-17T04:25:44.586664'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: closed
title: Add trial offset pagination
---

# Plan

Add trial offset pagination

The trials endpoint now supports limit, but no offset. Add simple offset pagination for large sweeps.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add optional query parameter `offset` to GET /api/trials.
- Default offset is 0.
- Reject negative offsets with HTTP 400.
- Apply offset after status filtering and before limit.
- Add optional offset argument to list_trials client.
- Add CLI option `--offset` to `jernerics trials`.
- Preserve current behavior when omitted.

Validation:
- .abraxas/validate.sh
