slurm = {
    "time": "0:30:00",
    "mem": "4G",
    "cpus-per-task": "2",
    "output": "results/slurm_%A_%a.out",
    "error": "results/slurm_%A_%a.err",
    "max_parallel": 5,
}

max_workers = 4

configs = [
    {"seed": 1, "lr": 0.001},
    {"seed": 2, "lr": 0.001},
    {"seed": 3, "lr": 0.01},
]
