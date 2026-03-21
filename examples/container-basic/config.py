slurm = {
    "time": "0:10:00",
    "mem": "2G",
    "cpus-per-task": "1",
    "output": "results/slurm_%A_%a.out",
}

configs = [
    {"output_dir": "results/run1", "samples": 100, "features": 10},
    {"output_dir": "results/run2", "samples": 200, "features": 20},
]
