---
base_branch: main
branch: null
completed_at: '2026-06-17T05:00:04.584306'
created: '2026-06-17T04:25:36.319433'
merge_commit_sha: 97b96cbd315ee2c637f5597ac9346af40b9299b5
parent_id: null
retry_count: 0
status: closed
title: Add typed sweep summary endpoint and CLI command
---

# Plan

Add typed sweep summary endpoint and CLI command

There is a compare endpoint for two sweeps, but no typed single-sweep summary endpoint exposing counts and available keys. Add a factual summary view.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add GET /api/sweep-summary?project=<project>&study_name=<study_name>.
- Return 404 if the sweep does not exist.
- Return JSON with:
  - project
  - study_name
  - trial_count
  - completed_count
  - param_keys
  - final_metric_keys
  - artifact_keys
- Add HTTP client function get_sweep_summary(base_url, project, study_name) -> dict.
- Add CLI command `jernerics sweep-summary --sweep <study_name>`.
- Add optional --project, --server, and --json.
- Human output should be concise Rich tables/lists; do not interpret quality.
- Do not call /query.

Validation:
- .abraxas/validate.sh
