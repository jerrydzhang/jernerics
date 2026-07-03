lint:
    ruff check .

lint-fix:
    ruff check --fix .

format:
    ruff format .

format-check:
    ruff format --check .

typecheck:
    ty check

test:
    pytest

test-unit:
    pytest packages/jernerics/tests/unit packages/jernerics-server/tests

test-remote:
    #!/usr/bin/env bash
    JERNERICS_RUN_REMOTE=1 pytest tests/e2e_remote/test_pueue_remote.py & p=$!
    JERNERICS_RUN_REMOTE=1 pytest tests/e2e_remote/test_hpc.py & h=$!
    wait "$p"; ps=$?
    wait "$h"; hs=$?
    if [ "$ps" -ne 0 ] || [ "$hs" -ne 0 ]; then exit 1; fi

check: lint format-check typecheck test

install-skills:
    mkdir -p ~/.pi/agent/skills
    ln -sfn {{justfile_directory()}}/skills/jernerics ~/.pi/agent/skills/jernerics
