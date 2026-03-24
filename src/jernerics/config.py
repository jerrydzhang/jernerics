from __future__ import annotations

from typing import Any


def merge_configs(
    base: dict[str, Any],
    overrides: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [{**base, **override} for override in overrides]
