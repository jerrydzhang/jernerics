import json
from pathlib import Path
from typing import Any


def fake_loss(lr: float, dropout: float, seed: int) -> float:
    import numpy as np

    rng = np.random.RandomState(seed)
    loss = (np.log10(lr) + 3.0) ** 2 + (1.0 - dropout) ** 2 + rng.normal(0, 0.01)
    return max(loss, 0.0)


def crash_transient(trial_number: int, crash_on: list[int]) -> bool:
    """Crash specific trial numbers. Retries get new numbers → succeed."""
    return trial_number in crash_on


def crash_permanent(lr: float) -> bool:
    """Crash when lr is very small. Same params on retry → same crash."""
    return lr < 5e-4


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
