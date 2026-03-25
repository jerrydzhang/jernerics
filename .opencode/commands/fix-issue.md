---
description: Fix a single GitHub issue with branch and PR
---

Fix GitHub issue #$ARGUMENTS.

If --skip-discussion is present skip step 2.

Steps:
1. Fetch issue: `gh issue view $ARGUMENTS --json title,body,number`
2. Discuss approach with user (e.g., worktree setup, implementation strategy) - always pause here unless --skip-discussion
3. Create branch from main: `git checkout main && git pull && git checkout -b issue-$ARGUMENTS-<slug>`
4. Understand and fix the issue
5. Run validation: `pytest`, `ruff check .`, `ty check`
6. Commit: `git commit -m "fix: <description> (fixes #$ARGUMENTS)"`
7. Push and create PR: `gh pr create --title "<title>" --body "Fixes #$ARGUMENTS"`
