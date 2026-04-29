import json
import os
from pathlib import Path
from typing import Any


def fake_loss(lr: float, dropout: float, seed: int) -> float:
    import numpy as np

    rng = np.random.RandomState(seed)
    loss = (np.log10(lr) + 3.0) ** 2 + (1.0 - dropout) ** 2 + rng.normal(0, 0.01)
    return max(loss, 0.0)


def crash_app(trial_number: int, crash_on: list[int]) -> bool:
    """App-level crash. Optuna records FAIL, checker submits fresh trials."""
    return trial_number in crash_on


def crash_node(
    trial_number: int,
    crash_on: list[int],
    lr: float = 0,
    lr_fatal: float = -1,
) -> bool:
    """Simulate node death. Kills process immediately.

    By trial number (crash_on): specific trials die, retries get new
    numbers and survive.

    By param value (lr_fatal): any trial with lr < lr_fatal dies.
    Retried trials get the same params via enqueue_trial, so they also
    die — until max_retries is exhausted.
    """
    if trial_number in crash_on:
        os._exit(9)
    if lr_fatal > 0 and lr < lr_fatal:
        os._exit(9)
    return False


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
