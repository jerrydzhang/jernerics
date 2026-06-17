---
base_branch: main
branch: abraxas/add-metric-key-filtering-to-trials-endpoint-and-cli
completed_at: '2026-06-17T00:58:02.966452'
created: '2026-06-17T00:03:55.434796'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: closed
title: Add metric-key filtering to trials endpoint and CLI
---

# Plan

Add metric-key filtering to trials endpoint and CLI

Large sweeps can have many final metrics. Add a narrow filter so users can inspect a subset without changing the full JSON contract by default.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add optional query parameter `metric_keys` to GET /api/trials as a comma-separated list.
- If omitted, return all final_metrics as today.
- If provided, include only those keys in each trial's final_metrics.
- Add optional metric_keys argument to list_trials and encode it in the URL.
- Add CLI option `--metrics key1,key2` to `jernerics trials`.
- The human table should show only selected final metric columns when --metrics is provided.
- JSON output should include endpoint data after filtering.

Validation:
- .abraxas/validate.sh
