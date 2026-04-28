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
    uv run pytest

test-unit:
    uv run pytest tests/unit

proto:
    cd packages/jernerics-proto && uv run python generate.py

check: lint format-check typecheck test
