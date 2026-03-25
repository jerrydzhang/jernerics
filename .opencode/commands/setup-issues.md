---
description: Create ralph tasks from open GitHub issues
---

Fetch open issues with `gh issue list --json number,title --state open`.

Create `.ralph/ralph-tasks.md` with:

```
# Issues to Fix

- [ ] #<number>: <title>
- [ ] #<number>: <title>
...
```

Then tell the user to run:
`ralph "Read .ralph/ralph-tasks.md. Pick the first incomplete [ ] task, fix that issue (create branch issue-<num>-<slug>, implement fix, run pytest/ruff check ./ty check, commit, push, create PR with 'Fixes #<num>'), then mark it [x] in the file. Output <promise>READY_FOR_NEXT_TASK</promise> when PR is created." --tasks`
