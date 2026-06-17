---
base_branch: null
branch: null
created: '2026-06-17T04:25:34.211876'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: Add default timeout to tracking HTTP API client
---

# Plan

Add default timeout to tracking HTTP API client

The observability HTTP client currently calls urlopen without a timeout, so CLI commands can hang indefinitely when a server accepts a connection but does not respond.

Files:
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/tracking/test_http_api.py

Behavior:
- Add a module-level default timeout of 30 seconds for HTTP requests.
- Pass the timeout to urlopen in the internal request helper.
- Keep public client function signatures unchanged.
- Add a test proving urlopen is called with the timeout.
- Do not add retries or async behavior.

Validation:
- .abraxas/validate.sh
