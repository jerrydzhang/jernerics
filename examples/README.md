# DAG Example

This example demonstrates all DAG functionality:

- **Parallel execution**: `preprocess_a` and `preprocess_b` run concurrently
- **Dependencies**: Tasks wait for their dependencies
- **Config injection**: Each task receives `config` with sweep parameters
- **State persistence**: `.jernerics/` dir enables resume after crash
- **SLURM array jobs**: Each config runs as a separate array task

## DAG Structure

```
load_data
    ├── preprocess_a ──► train_model_a ──┐
    │                                    ├──► compare_models ──► finalize
    └── preprocess_b ──► train_model_b ──┘
```

## Run Locally

```bash
cd examples
jernerics run local dag.py config.py
```

## Run on SLURM

```bash
cd examples
jernerics run slurm dag.py config.py
```

Override SLURM options:

```bash
jernerics run slurm dag.py config.py --set time=1:00:00 --set mem=8G
```

## Expected Output

After running, check `results/run_{seed}/` for:
- `data.json` - Raw data info
- `preprocess_a.json` / `preprocess_b.json` - Preprocessing results
- `model_a.json` / `model_b.json` - Trained models
- `comparison.json` - Model comparison
- `summary.json` - Final summary
