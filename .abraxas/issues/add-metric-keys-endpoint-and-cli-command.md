---
base_branch: main
branch: abraxas/add-metric-keys-endpoint-and-cli-command
completed_at: '2026-06-17T05:42:33.209165'
created: '2026-06-17T04:25:40.437434'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: closed
title: Add metric keys endpoint and CLI command
---

# Plan

Add metric keys endpoint and CLI command

Metric-history requires knowing a metric key, but there is no direct typed command to list available final metric keys for a sweep.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add GET /api/metric-keys?project=<project>&study_name=<study_name>.
- Return JSON object with:
  - project
  - study_name
  - final_metric_keys
- Return 404 if the sweep does not exist.
- Add HTTP client function list_metric_keys(base_url, project, study_name) -> dict.
- Add CLI command `jernerics metric-keys --sweep <study_name>`.
- Add optional --project, --server, and --json.
- Human output should list final metric keys one per row.
- Do not call /query.

Validation:
- .abraxas/validate.sh
