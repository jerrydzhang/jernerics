from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from gpu_smoke import check_gpu, save_json
from jernerics.dag import DAG, task

with DAG() as dag:

    @task
    def detect_gpu(config):
        output_dir = Path(config["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        gpu_info = check_gpu()
        save_json(str(output_dir / "gpu_info.json"), gpu_info)
        return gpu_info

    @task(depends_on=[detect_gpu])
    def run_compute(detect_gpu, config):
        output_dir = Path(config["output_dir"])
        import torch

        device = "cuda" if detect_gpu["cuda_available"] else "cpu"
        x = torch.randn(1000, 1000, device=device)
        y = torch.mm(x, x)
        result = {
            "device": device,
            "tensor_shape": list(y.shape),
            "sum": float(y.sum()),
        }
        save_json(str(output_dir / "compute.json"), result)
        return result

    @task(depends_on=[run_compute])
    def finalize(run_compute, config):
        output_dir = Path(config["output_dir"])
        summary = {
            "status": "completed",
            "device_used": run_compute["device"],
        }
        save_json(str(output_dir / "summary.json"), summary)
        return summary
