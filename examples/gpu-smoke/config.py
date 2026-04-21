from __future__ import annotations

_base = {"output_dir": "results/gpu_test"}

n_trials = 2
objective_task = None
objective_metric = None
direction = "minimize"

slurm = {
    "time": "0:10:00",
    "mem": "8G",
    "cpus-per-task": "2",
    "gres": "gpu:1",
    "partition": "priority-gpu",
}
