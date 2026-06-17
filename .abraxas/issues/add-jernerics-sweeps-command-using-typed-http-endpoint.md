---
base_branch: null
branch: null
created: '2026-06-16T22:31:03.972062'
merge_commit_sha: null
order: 2
parent_id: null
retry_count: 0
status: open
title: Add jernerics sweeps command using typed HTTP endpoint
---

# Plan

Add jernerics sweeps command using typed HTTP endpoint

Add the first CLI observability command backed by GET /api/sweeps.

Files:
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/test_cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py

Behavior:
- Create a small stdlib urllib-based HTTP API client.
- Implement list_sweeps(base_url: str) -> list[dict].
- If JERNERICS_API_KEY is set, send Authorization: Bearer <key>.
- Add command: jernerics sweeps.
- Add option --server <url>.
- If --server is omitted, use JERNERICS_TRACKING_HTTP_SERVER.
- If env is omitted, read [tool.jernerics].tracking_http_server when pyproject.toml exists.
- If no server URL is available, exit CONFIG_ERROR with message mentioning --server, JERNERICS_TRACKING_HTTP_SERVER, and tracking_http_server.
- Default output should be a Rich table with project, study_name, trials, completed, last_event.
- Add --json flag that prints exact JSON from the typed endpoint.
- Do not call /query.
- Do not add JERNERICS_TRACKING_HTTP_SERVER to ARTIFACT_ENV_VARS.

Validation:
- .abraxas/validate.sh
