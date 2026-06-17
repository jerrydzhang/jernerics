---
base_branch: null
branch: null
created: '2026-06-16T22:31:10.131288'
merge_commit_sha: null
order: 5
parent_id: null
retry_count: 0
status: open
title: Add typed compare-sweeps endpoint
---

# Plan

Add typed compare-sweeps endpoint

Add a factual cross-sweep comparison endpoint.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/tests/test_http.py

Behavior:
- Add GET /api/compare-sweeps?project=<project>&left=<study>&right=<study>.
- Return JSON with:
  - left and right study names
  - trial_count and completed_count for each
  - param_keys shared/left_only/right_only
  - final_metric_keys shared/left_only/right_only
  - artifact_keys shared/left_only/right_only
  - for shared final metric keys, min/median/max per sweep
- Compute median in Python if SQLite support is awkward.
- Do not say which sweep is better.
- Respect bearer auth.
- Do not expose raw SQL.

Validation:
- .abraxas/validate.sh
