---
base_branch: null
branch: null
created: '2026-06-16T22:31:06.021646'
merge_commit_sha: null
order: 3
parent_id: null
retry_count: 0
status: open
title: Add typed trials endpoint to tracking server
---

# Plan

Add typed trials endpoint to tracking server

Add a typed HTTP endpoint for listing trial-level data for one sweep.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/tests/test_http.py

Behavior:
- Add GET /api/trials?project=<project>&study_name=<study_name>.
- Return JSON list with one object per trial.
- Each trial object should include:
  - trial_id
  - status: "complete" if trial_end exists, otherwise "incomplete"
  - params: object mapping param keys to scalar values
  - final_metrics: object mapping metric keys to values where step IS NULL
  - artifact_keys: list of artifact keys
- Sort by trial_id.
- Respect bearer auth.
- Do not expose raw SQL.

Validation:
- .abraxas/validate.sh
