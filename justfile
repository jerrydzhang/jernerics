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

proto:
    cd packages/jernerics-proto && python generate.py

check: lint format-check typecheck test

install-skills:
    mkdir -p ~/.pi/agent/skills
    ln -sfn {{justfile_directory()}}/skills/jernerics ~/.pi/agent/skills/jernerics
