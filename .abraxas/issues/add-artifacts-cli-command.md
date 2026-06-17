---
base_branch: main
branch: null
completed_at: '2026-06-17T02:04:12.399055'
created: '2026-06-17T01:14:56.861843'
merge_commit_sha: fef3b8110c457586cdbb1a0e7a0567ce9bd12325
parent_id: null
retry_count: 0
status: closed
title: Add artifacts CLI command
---

# Plan

Add artifacts CLI command

Add a CLI command for listing tracked artifacts from the typed HTTP API.

Files:
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Add HTTP client function list_artifacts(base_url, project, study_name, trial_id=None) -> list[dict].
- URL-encode all query parameters and reuse existing HTTP error handling.
- Add command `jernerics artifacts --sweep <study_name>`.
- Add optional --project with default-to-current-project behavior.
- Add optional --trial-id filter.
- Add --server with existing observability server resolution behavior.
- Default human output should be a Rich table with trial_id, key, filename, timestamp_ns.
- Add --json to print exact endpoint data.
- Do not call /query.

Validation:
- .abraxas/validate.sh
