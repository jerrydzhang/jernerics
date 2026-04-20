# Branch: optuna-mlflow-integration — TODO

Pre-merge checklist. Delete this file before merging to main.

## Must Do

- [ ] **Revert example pyproject.toml sources** — `examples/sweep-basic/pyproject.toml` (and any other examples touched during testing) should point back to the git branch, not local paths. Verify all examples still reference `git = "https://github.com/jerrydzhang/jernerics.git"` with `branch = "optuna-mlflow-integration"` (or remove branch pin after merge).

- [ ] **Add `mlflow` dependency to examples that need it** — `examples/sweep-basic/pyproject.toml` has `mlflow` added. Decide if other sweep examples (`sweep-parallel`, `no-objective-sweep`) should also list it, or if it's only needed when `[tool.jernerics.mlflow]` is configured.

- [ ] **Add `mlflow.log_artifact` support** — The current integration only logs params and metrics. Users need a way to log artifacts (model checkpoints, plots, etc.) from within DAG tasks. This is the last piece before the `jernerics results` download command can be superseded by mlflow for experiment outputs.

- [ ] **NixOS module for mlflow tracking server** — Add a NixOS module to `flake.nix` that exposes an `mlflow server` as a systemd service. Needed for the HPC → home server use case. See the context prompt generated during the mlflow local testing session for full details.

- [ ] **Decide on auth strategy for `JERNERICS_MLFLOW_PASSWORD`** — Currently the SLURM script does `export MLFLOW_TRACKING_PASSWORD=${JERNERICS_MLFLOW_PASSWORD}` but nothing sets that env var. Need a plan: either inject via SLURM `--export`, user's shell profile on the cluster, or the NixOS module handles it. Document the chosen approach.

## Nice to Have

- [ ] **Remove `run_local`'s duplicate study creation** — `run_local` creates the optuna study in the parent process, then the subprocess runner does `optuna.create_study(..., load_if_exists=True)` again. Works but is redundant now that the runner always creates with `load_if_exists`. Could simplify.
