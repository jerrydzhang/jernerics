---
base_branch: main
branch: null
completed_at: '2026-06-17T00:29:43.437618'
created: '2026-06-17T00:03:51.294218'
merge_commit_sha: b10cb87b0cd81dc0de61d87bad2b6e00ed6c9230
parent_id: null
retry_count: 0
status: closed
title: Add project filtering to sweeps endpoint and CLI
---

# Plan

Add project filtering to sweeps endpoint and CLI

Allow users to list sweeps for one project without client-side filtering.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add optional query parameter `project` to GET /api/sweeps.
- If project is provided, return only sweeps for that project.
- Add optional `project` parameter to list_sweeps(base_url, project=None) and URL-encode it.
- Add `jernerics sweeps --project <project>`.
- If --project is omitted, keep current behavior of showing all projects.
- Preserve --json behavior.

Validation:
- .abraxas/validate.sh
