---
## Goal

Create a CLI-driven workflow for running experiments on HPC (SLURM + Apptainer) with minimal per-project setup, agent-safe operations, and reproducible builds.

## Instructions

- Read the full design plan: `.opencode/hpc-cli-plan.md`
- No external container registries - build on HPC only
- All SSH operations scoped to configured remote_dir
- No compute on login node - always use SLURM
- Templates: `gpu` and `cpu` for Python projects

## Current State

- GPU example working at `examples/container-gpu/`
- Container builds via SLURM job, runs DAG with GPU access
- Manual scripts (`test_sync_run.sh`, `run.sh`) - need to be replaced by CLI

## Next Step

Implement Phase 1 of the plan: `jernerics container build` command

## Relevant Files

```
.opencode/hpc-cli-plan.md     # Full design document
examples/container-gpu/       # Working reference implementation
  ├── container.def           # Current Apptainer definition
  ├── pyproject.toml          # Dependencies + jernerics git pin
  ├── test_sync_run.sh        # Current sync + build script (to be replaced)
  └── run.sh                  # Current run script (to be replaced)

src/jernerics/
├── cli.py                    # CLI entry point
└── _cli_helpers.py           # Config loading
```
