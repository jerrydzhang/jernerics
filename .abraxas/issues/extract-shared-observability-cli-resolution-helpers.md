---
base_branch: main
branch: null
completed_at: '2026-06-17T03:12:43.782577'
created: '2026-06-17T02:28:04.151598'
merge_commit_sha: f59f73c9e3a2729bd583d796a667c24c77185465
parent_id: null
retry_count: 0
status: closed
title: Extract shared observability CLI resolution helpers
---

# Plan

Extract shared observability CLI resolution helpers

The observability commands repeat the same tracking HTTP server resolution and current-project default logic. Extract small helpers to reduce drift without changing behavior.

Files:
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add a private helper for resolving the tracking HTTP server URL from --server, JERNERICS_TRACKING_HTTP_SERVER, or [tool.jernerics].tracking_http_server.
- Add a private helper for resolving project name from optional --project, defaulting to current pyproject.toml when needed.
- Update sweeps, trials, compare-sweeps, metric-history, artifacts, results, and tracking-health to use the helper where applicable.
- Preserve current error messages and exit codes as closely as practical.
- This is a local cleanup only; do not move commands to another module.

Validation:
- .abraxas/validate.sh
