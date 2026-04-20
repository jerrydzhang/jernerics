from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def compute_model_stats(
    hidden_size: int,
    num_layers: int,
    input_dim: int = 784,
    num_classes: int = 10,
) -> dict[str, float]:
    total_params = input_dim * hidden_size + hidden_size  # first layer
    for _ in range(num_layers - 1):
        total_params += hidden_size * hidden_size + hidden_size  # hidden layers
    total_params += hidden_size * num_classes + num_classes  # output layer
    flops = total_params * 2  # rough: multiply-add = 2 ops
    return {"flops": float(flops), "params_count": float(total_params)}


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
