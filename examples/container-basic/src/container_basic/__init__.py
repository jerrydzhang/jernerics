import json
from pathlib import Path


def load_json(path: str) -> dict:
    return json.loads(Path(path).read_text())


def save_json(path: str, data: dict) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, indent=2))


def process_data(samples: int, features: int) -> dict:
    return {
        "processed": True,
        "samples": samples,
        "features": features,
        "mean": samples * features / 100,
    }
