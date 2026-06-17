---
base_branch: main
branch: abraxas/add-typed-results-endpoint-and-cli-command
completed_at: '2026-06-17T02:26:54.741027'
created: '2026-06-17T01:14:58.917462'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: closed
title: Add typed results endpoint and CLI command
---

# Plan

Add typed results endpoint and CLI command

Tracked results are stored in the server but not exposed through typed observability APIs. Add a factual listing endpoint and CLI command.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add GET /api/results?project=<project>&study_name=<study_name>.
- Add optional query parameters trial_id and key.
- Return JSON list sorted by trial_id, key with objects containing:
  - trial_id
  - key
  - value
  - timestamp_ns
- Values should remain the stored JSON string for now; do not interpret them.
- Add HTTP client function list_results(base_url, project, study_name, trial_id=None, key=None).
- Add CLI command `jernerics results --sweep <study_name>`.
- Add optional --project, --trial-id, --key, --server, and --json.
- Default human output should be a Rich table with trial_id, key, value, timestamp_ns.
- Do not call /query.

Validation:
- .abraxas/validate.sh
