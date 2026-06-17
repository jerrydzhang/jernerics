---
base_branch: main
branch: null
completed_at: '2026-06-17T04:02:23.016526'
created: '2026-06-17T02:28:08.312877'
merge_commit_sha: 7b9efed332fef8c67573452705ad06eded51e05c
parent_id: null
retry_count: 0
status: closed
title: Add typed params endpoint and CLI command
---

# Plan

Add typed params endpoint and CLI command

Parameters are visible through trials, but there is no typed endpoint for listing parameter observations directly. Add a factual params view.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add GET /api/params?project=<project>&study_name=<study_name>.
- Add optional query parameters trial_id and key.
- Return JSON list sorted by trial_id, key with objects containing:
  - trial_id
  - key
  - value, using the stored scalar value with bools as booleans
  - timestamp_ns
- Add HTTP client function list_params(base_url, project, study_name, trial_id=None, key=None).
- Add CLI command `jernerics params --sweep <study_name>`.
- Add optional --project, --trial-id, --key, --server, and --json.
- Default human output should be a Rich table with trial_id, key, value, timestamp_ns.
- Do not call /query.

Validation:
- .abraxas/validate.sh
