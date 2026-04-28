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

check: lint format-check typecheck test
