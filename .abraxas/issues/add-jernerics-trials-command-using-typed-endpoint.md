---
base_branch: main
branch: null
completed_at: '2026-06-16T23:33:25.793489'
created: '2026-06-16T22:31:08.076625'
merge_commit_sha: 6876fe78f658f86226ed44850b54b978fb31b02e
order: 4
parent_id: null
retry_count: 0
status: closed
title: Add jernerics trials command using typed endpoint
---

# Plan

Add jernerics trials command using typed endpoint

Add a CLI command for inspecting trials in one sweep.

Files:
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/src/jernerics/tracking/http_api.py
- packages/jernerics/tests/unit/test_cli.py
- packages/jernerics/tests/unit/tracking/test_http_api.py

Behavior:
- Add HTTP client function list_trials(base_url: str, project: str, study_name: str).
- Add command: jernerics trials --project <project> --sweep <study_name>.
- Add option --server <url>, with same resolution as jernerics sweeps.
- Default human output should include:
  - trial_id
  - status
  - final metric columns
  - artifact_count
- Do not include every param column by default.
- Add --params flag to include param columns in human table.
- Add --columns option as comma-separated projection over displayed columns.
- Add --limit option default 100.
- Add --json flag that prints full endpoint data.
- Do not call /query.

Validation:
- .abraxas/validate.sh
