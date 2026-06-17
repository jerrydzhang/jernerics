---
base_branch: null
branch: null
created: '2026-06-17T00:03:53.354678'
merge_commit_sha: null
parent_id: null
retry_count: 0
status: open
title: Default trials and compare commands to current project
---

# Plan

Default trials and compare commands to current project

The trials and compare-sweeps commands currently require --project even when run inside a configured Jernerics project. Make the common case easier while preserving explicit override.

Files:
- packages/jernerics/src/jernerics/cli.py
- packages/jernerics/tests/unit/test_cli.py

Behavior:
- Make --project optional for `jernerics trials` and `jernerics compare-sweeps`.
- If --project is omitted, find pyproject.toml and use get_project_name(project_dir).
- If no pyproject.toml is found and --project was omitted, exit CONFIG_ERROR with a message asking for --project or a Jernerics project directory.
- If --project is provided, keep current behavior and do not require pyproject.toml except for resolving the server config when --server/env is absent.
- Do not change endpoint behavior.

Validation:
- .abraxas/validate.sh
