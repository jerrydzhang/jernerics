---
base_branch: null
branch: null
created: '2026-06-16T22:38:42.811341'
merge_commit_sha: null
order: 0
parent_id: null
retry_count: 0
status: open
title: Bootstrap Abraxas validation for Jernerics after transient provider failures
---

# Plan

Bootstrap Abraxas validation for Jernerics after transient provider failures

Set up the project validation contract Abraxas expects, and fix the broken unit test recipe. This replaces the previous stuck bootstrap issue that exhausted retries due to provider 429 errors.

Files:
- justfile
- .abraxas/validate.sh

Behavior:
- Fix `just test-unit`, which currently runs `pytest tests/unit` but that path does not exist.
- It should run the existing unit test locations:
  - packages/jernerics/tests/unit
  - packages/jernerics-server/tests
- Add `.abraxas/validate.sh`.
- The script should be executable.
- It should run the full deterministic project validation:
  - just lint
  - just format-check
  - just typecheck
  - just test
- Use `set -euo pipefail`.
- Do not use uv run or uv sync.
- Do not create a venv.

Validation:
- .abraxas/validate.sh
