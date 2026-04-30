lint:
    uv run ruff check .

lint-fix:
    uv run ruff check --fix .

format:
    uv run ruff format .

format-check:
    uv run ruff format --check .

typecheck:
    uv run ty check

test:
    uv run pytest

test-unit:
    uv run pytest tests/unit

proto:
    cd packages/jernerics-proto && uv run python generate.py

check: lint format-check typecheck test
