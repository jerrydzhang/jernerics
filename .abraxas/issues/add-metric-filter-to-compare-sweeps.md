---
base_branch: null
branch: null
created: '2026-06-17T02:28:10.374540'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: Add metric filter to compare-sweeps
---

# Plan

Add metric filter to compare-sweeps

For sweeps with many shared final metrics, compare-sweeps output can be too wide/noisy. Add an optional metric filter while keeping the factual default unchanged.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add optional query parameter `metrics` to GET /api/compare-sweeps as a comma-separated list.
- When provided, restrict `final_metric_stats` to those metric keys, but leave `final_metric_keys` overlap information unchanged.
- Add optional metrics argument to compare_sweeps client and URL-encode it.
- Add CLI option `--metrics key1,key2` to `jernerics compare-sweeps`.
- Do not interpret which sweep is better.

Validation:
- .abraxas/validate.sh
