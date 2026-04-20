from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def check_gpu() -> dict[str, Any]:
    import torch

    return {
        "cuda_available": torch.cuda.is_available(),
        "cuda_version": torch.version.cuda if torch.cuda.is_available() else None,
        "device_count": torch.cuda.device_count() if torch.cuda.is_available() else 0,
        "device_name": torch.cuda.get_device_name(0)
        if torch.cuda.is_available()
        else None,
    }


def save_json(path: str | Path, data: dict[str, Any]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))
