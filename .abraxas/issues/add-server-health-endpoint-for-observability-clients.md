---
base_branch: null
branch: null
created: '2026-06-17T00:03:57.481448'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: Add server health endpoint for observability clients
---

# Plan

Add server health endpoint for observability clients

Add a simple typed health endpoint so CLI/users can distinguish an unreachable server from a reachable Jernerics tracking HTTP service.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/tests/test_http.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add GET /api/health returning JSON object with at least `{ "ok": true }`.
- Respect bearer auth consistently with other /api endpoints.
- Add HTTP client function get_health(base_url: str) -> dict.
- Add CLI command `jernerics tracking-health` with --server option and the same server resolution behavior as other observability commands.
- Default output should be a short human-readable success line; add --json to print the response.
- Do not call /query.

Validation:
- .abraxas/validate.sh
