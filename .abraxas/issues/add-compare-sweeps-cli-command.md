---
base_branch: main
branch: abraxas/add-compare-sweeps-cli-command
completed_at: '2026-06-17T00:02:52.791994'
created: '2026-06-16T22:31:12.182809'
merge_commit_sha: null
order: 6
parent_id: null
retry_count: 0
status: closed
title: Add compare-sweeps CLI command
---

# Plan

Add compare-sweeps CLI command

Add a factual cross-sweep comparison command using the typed endpoint.

Files:
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/test_cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py

Behavior:
- Add HTTP client function compare_sweeps(base_url: str, project: str, left: str, right: str).
- Add command: jernerics compare-sweeps --project <project> <left> <right>.
- Add option --server <url>, with same resolution as jernerics sweeps.
- Default output should be factual side-by-side Rich tables.
- Include counts, key overlap, and shared final metric min/median/max.
- Add --json flag.
- Do not call /query.
- Do not interpret which sweep is better.

Validation:
- .abraxas/validate.sh
