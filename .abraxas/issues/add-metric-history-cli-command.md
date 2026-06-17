---
base_branch: main
branch: null
completed_at: '2026-06-17T01:45:36.033506'
created: '2026-06-17T01:14:52.714725'
merge_commit_sha: 105f010d5493ff811147cfa6142c6412934f0f20
parent_id: null
retry_count: 0
status: closed
title: Add metric-history CLI command
---

# Plan

Add metric-history CLI command

Add a CLI command for retrieving metric history from the typed tracking HTTP API.

Files:
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add HTTP client function get_metric_history(base_url, project, study_name, key) -> list[dict].
- URL-encode all query parameters and reuse existing HTTP error handling.
- Add command `jernerics metric-history --sweep <study_name> --metric <key>`.
- Add optional --project with the same default-to-current-project behavior as trials and compare-sweeps.
- Add --server with the same resolution behavior as other observability commands.
- Default human output should be a Rich table with trial_id, step, value, timestamp_ns.
- Add --json to print exact endpoint data.
- Do not call /query.

Validation:
- .abraxas/validate.sh
