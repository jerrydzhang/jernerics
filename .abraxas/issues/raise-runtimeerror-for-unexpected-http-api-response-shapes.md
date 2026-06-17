---
base_branch: main
branch: abraxas/raise-runtimeerror-for-unexpected-http-api-response-shapes
completed_at: '2026-06-17T03:01:05.811760'
created: '2026-06-17T02:28:02.078164'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: closed
title: Raise RuntimeError for unexpected HTTP API response shapes
---

# Plan

Raise RuntimeError for unexpected HTTP API response shapes

The observability HTTP client currently raises TypeError when the server returns a valid JSON value with the wrong shape. CLI commands only catch RuntimeError, so these shape errors can surface as tracebacks.

Files:
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/src/jernerics/cli.py if needed

Behavior:
- Change list_sweeps, list_trials, compare_sweeps, get_metric_history, get_health, list_artifacts, and list_results so unexpected JSON shapes raise RuntimeError with a clear message.
- Keep the existing successful return types.
- Add tests for at least one list endpoint and one dict endpoint returning the wrong shape.
- CLI commands should continue catching RuntimeError and exiting GENERAL_ERROR.

Validation:
- .abraxas/validate.sh
