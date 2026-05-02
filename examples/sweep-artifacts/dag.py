from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from jernerics.dag import DAG, task
from jernerics.tracking.tracker import Tracker

with DAG() as dag:

    @task
    def write_artifact(config, tracker: Tracker):
        out_dir = Path("artifacts-out")
        out_dir.mkdir(exist_ok=True)

        text_file = out_dir / f"summary-{config['config_index']}.txt"
        text_file.write_text(f"Trial {config['config_index']}, seed={config['seed']}\n")

        tracker.log_artifact(f"summary-{config['config_index']}.txt", str(text_file))
        return {"artifact_path": str(text_file)}
