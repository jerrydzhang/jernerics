---
base_branch: main
branch: abraxas/add-consistent-http-api-error-messages
completed_at: '2026-06-17T00:18:20.672211'
created: '2026-06-17T00:03:49.244152'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: closed
title: Add consistent HTTP API error messages
---

# Plan

Add consistent HTTP API error messages

Make observability CLI failures understandable when the tracking HTTP server returns an error or unreachable response.

Files:
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/src/jernerics/cli.py if needed

Behavior:
- Add a small internal request helper used by list_sweeps, list_trials, and compare_sweeps.
- Preserve Authorization: Bearer behavior from JERNERICS_API_KEY.
- On HTTPError, raise RuntimeError with the status code and any JSON `detail` or `error` field from the response body.
- On URLError, raise RuntimeError mentioning the base server URL and the original reason.
- On invalid JSON, raise RuntimeError that the server returned invalid JSON.
- CLI commands should print these RuntimeError messages and exit GENERAL_ERROR.
- Keep implementation small; do not add retries or async clients.

Validation:
- .abraxas/validate.sh
