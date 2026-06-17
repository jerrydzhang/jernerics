---
base_branch: null
branch: null
created: '2026-06-17T00:03:47.196953'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: URL-encode observability HTTP client query parameters
---

# Plan

URL-encode observability HTTP client query parameters

The new tracking HTTP API client builds query strings by interpolating raw project and study names. This breaks names containing spaces, slashes, plus signs, ampersands, or other reserved URL characters.

Files:
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/tracking/test_http_api.py

Behavior:
- Use stdlib URL encoding for all query parameters in list_trials and compare_sweeps.
- Keep list_sweeps unchanged except for any shared helper needed.
- Add tests proving project/study names with spaces and `&` are encoded correctly.
- Do not introduce a third-party dependency.

Validation:
- .abraxas/validate.sh
