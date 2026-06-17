---
base_branch: null
branch: null
created: '2026-06-17T02:28:12.439282'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: Document tracking HTTP observability commands
---

# Plan

Document tracking HTTP observability commands

The new typed tracking HTTP API and CLI commands are user-facing but undocumented. Add concise README documentation.

Files:
- README.md

Behavior:
- Add a section for tracking HTTP observability.
- Explain that commands use the tracking HTTP server, not the gRPC tracking_server address.
- Document server URL resolution: --server, JERNERICS_TRACKING_HTTP_SERVER, then [tool.jernerics].tracking_http_server.
- List the commands added in this work: sweeps, trials, compare-sweeps, metric-history, artifacts, results, params if present, and tracking-health.
- Mention --json for agent/script consumption.
- Keep the docs concise and factual; do not describe future dashboard/plugin plans.

Validation:
- .abraxas/validate.sh
