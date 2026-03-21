slurm = {
    "time": "0:30:00",
    "mem": "8G",
    "cpus-per-task": "4",
    "gres": "gpu:1",
    "partition": "priority-gpu",
    "output": "results/slurm_%A_%a.out",
}

configs = [
    {"output_dir": "results/gpu_test"},
]
