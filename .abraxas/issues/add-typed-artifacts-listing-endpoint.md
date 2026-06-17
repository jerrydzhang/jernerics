---
base_branch: main
branch: abraxas/add-typed-artifacts-listing-endpoint
completed_at: '2026-06-17T01:53:07.911291'
created: '2026-06-17T01:14:54.809566'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: closed
title: Add typed artifacts listing endpoint
---

# Plan

Add typed artifacts listing endpoint

Artifacts are already stored and proxyable, but there is no typed endpoint for discovering them. Add a read-only listing endpoint.

Files:
- packages/jernerics-server/src/jernerics_server/http.py
- packages/jernerics-server/src/jernerics_server/store.py
- packages/jernerics-server/tests/test_http.py

Behavior:
- Add GET /api/artifacts?project=<project>&study_name=<study_name>.
- Add optional query parameter trial_id.
- Return JSON list sorted by trial_id, key with objects containing:
  - trial_id
  - key
  - filename
  - timestamp_ns
- If trial_id is provided, return only artifacts for that trial.
- Return an empty list if none exist.
- Respect bearer auth.
- Do not expose SQL to the client.

Validation:
- .abraxas/validate.sh
