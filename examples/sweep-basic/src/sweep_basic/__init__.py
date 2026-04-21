from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def fake_loss(lr: float, dropout: float, seed: int) -> float:
    import numpy as np

    rng = np.random.RandomState(seed)
    # Minimum at lr=0.001, dropout=1.0
    loss = (np.log10(lr) + 3.0) ** 2 + (1.0 - dropout) ** 2 + rng.normal(0, 0.01)
    return max(loss, 0.0)


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())
