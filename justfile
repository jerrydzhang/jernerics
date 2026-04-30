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
    pytest tests/unit

proto:
    cd packages/jernerics-proto && python generate.py

check: lint format-check typecheck test
