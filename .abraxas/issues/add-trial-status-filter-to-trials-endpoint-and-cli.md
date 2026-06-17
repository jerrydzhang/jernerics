---
base_branch: main
branch: null
completed_at: '2026-06-17T05:16:58.186977'
created: '2026-06-17T04:25:38.379590'
merge_commit_sha: 14d4989a215146c8d38ac6a933b886d35d20583a
parent_id: null
retry_count: 0
status: closed
title: Add trial status filter to trials endpoint and CLI
---

# Plan

Add trial status filter to trials endpoint and CLI

Users often want to inspect only completed or incomplete trials. Add a typed status filter.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add optional query parameter `status` to GET /api/trials.
- Accepted values: complete, incomplete.
- Reject any other value with HTTP 400.
- Apply the filter after status is computed and before limit is applied.
- Add optional status argument to list_trials client.
- Add CLI option `--status complete|incomplete` to `jernerics trials`.
- Preserve existing default behavior when omitted.

Validation:
- .abraxas/validate.sh
