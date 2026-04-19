# Handoff: examples/sweep-basic

## What this example is

An end-to-end Optuna hyperparameter sweep using a synthetic loss surface (no real ML). The DAG has 3 tasks: `generate_data` → `train` → `evaluate`. Optuna minimizes `evaluate.loss` over 20 trials by tuning `lr` and `dropout`. The known optimum is at lr=0.001, dropout=1.0 giving loss≈0.

## Current state

### Done
- All example files exist: `dag.py`, `config.py`, `src/sweep_basic/__init__.py`, `container.def`, `pyproject.toml`, `README.md`
- Core jernerics changes committed + pushed to `optuna-mlflow-integration` branch (rebased onto main as of `a0e7719`)
- **Local e2e verified**: `jernerics run local dag.py config.py` runs 20 trials, TPE converges to lr≈0.001, dropout≈1.0, best loss ~0.005
- **SLURM dry-run verified**: script has `#SBATCH --array=1-20%10`, `study.ask()`, `study.tell(trial, value)`, `sqlite:///`, `project_name='sweep-basic'`
- All 442 tests pass (excluding pre-existing broken `tests/integration/test_workflow.py`)
- `uv.lock` updated to point to the rebased branch tip (`a0e7719`)

### NOT done
- Container build on HPC with the new `/dev/shm` approach
- `jernerics run slurm dag.py config.py` actual submission
- Remote result verification
- Testing `--clean-logs` flag on `jernerics results`
- Committing example files to the repo

## Remaining steps

```bash
cd examples/sweep-basic

# 1. Build container (now uses /dev/shm for fast sandbox)
uv run jernerics container build --force
uv run jernerics logs <job_id> --follow

# 2. Submit sweep
uv run jernerics run slurm dag.py config.py
uv run jernerics logs <job_id> --follow

# 3. Verify results
uv run jernerics results <job_id> --clean-logs   # test --clean-logs flag
# SSH in and check .jernerics/optuna/ for the SQLite DB, verify best params

# 4. Commit example files
cd ../..
git add examples/sweep-basic/
git commit -m "feat: add sweep-basic example"
```

## Pitfalls to know

### General jernerics pitfalls

1. **Always `cd` into the example directory first.** `jernerics` reads `pyproject.toml` from CWD. If you run from the repo root, it reads the root pyproject.toml which has placeholder HPC config (`your-username@hpc.example.edu`) and fails with SSH errors.

2. **`uv run --reinstall-package jernerics` is required after changing jernerics source code.** The example venv installs jernerics from the built wheel, not the source tree. Without `--reinstall-package`, it uses a stale build. Alternatively, `uv lock --upgrade-package jernerics && uv sync` when the git branch changes.

3. **pyproject.toml `uv.sources.jernerics` must be git, not path, for container builds.** Inside the container, local path sources don't exist. The example points to `git = "https://github.com/jerrydzhang/jernerics.git"` with `branch = "optuna-mlflow-integration"`. For local dev against uncommitted changes, temporarily switch to `path = "../.."", then switch back before building the container.

4. **`test_workflow.py` is pre-existing broken.** It uses old `configs = [...]` format not supported by SweepConfig. Don't waste time on it.

5. **`load_jernerics_config` now returns 4 values** (base config, HPC config, container config, Mlflow config). Any caller that unpacks 3 values will fail.

### Container build pitfalls

6. **Container builds now use `/dev/shm` (node RAM) for the Apptainer sandbox.** This was fixed in PR #54 (commit `954f4ae`). Before this fix, builds used `/scratch` (wekafs network filesystem) which was catastrophically slow for metadata-heavy small-file operations — 60+ min vs 2.5 min on local storage. The `/dev/shm` approach means you need enough `mem` allocated to hold the sandbox (~2-6GB depending on dependencies). The `trap` in the generated script cleans up on exit.

7. **Container def must match actual project files.** The python template includes `README.md` in `%files`. If the project doesn't have one, the build fails with "cannot stat 'README.md'". Always create a minimal README or use a custom def without it.

8. **`APPTAINER_CACHEDIR` is no longer set.** The new `/dev/shm` approach only sets `APPTAINER_TMPDIR`. This is fine — the cache is only useful across builds and `/dev/shm` doesn't persist.

### Sweep-specific pitfalls

9. **`sampler` in config.py must be an actual Optuna sampler instance**, not a string. It gets pickled into the config namespace via `runpy.run_path()`.

10. **The SLURM sweep script uses `sqlite:///` for shared Optuna state** between array job workers. The SQLite DB lives at `.jernerics/optuna/` on the remote. The `remote_dir` trailing slash is stripped to prevent `//` in SQLite URLs.

11. **`~` expansion rules matter.** `~` expands in SSH commands (remote shell processes it) but NOT in SLURM directives, double-quoted strings, or heredocs. For those, use `$HOME` instead. See AGENTS.md for the full rules.

12. **`project_name` is passed to the DAG constructor** for MLflow tracking. In the SLURM sweep script, `project_name='sweep-basic'` is hardcoded from the pyproject.toml project name.

### HPC config for this project

```toml
[tool.jernerics.hpc]
host = "jez21005@hpc2.storrs.hpc.uconn.edu"
remote_dir = "~/projects/jernerics-examples/sweep-basic"
cache_dir = "/scratch/qiy18011/jez21005/jernerics"

[tool.jernerics.container]
partition = "priority"
mem = "4G"
time = "0:30:00"
cpus = 4
```

Note: `cache_dir` is used for bind mount cache directories (the `[tool.jernerics.binds]` feature), NOT for container builds anymore. Container builds use `/dev/shm` regardless of `cache_dir`.

## What this example tests

- SweepConfig with all fields populated
- search_space callable with suggest_float (log and linear)
- TPE Sampler instance in config
- objective_task + objective_metric extraction from dict result
- direction="minimize"
- Local sequential sweep loop with SQLite-based coordination
- SLURM sweep script generation (_generate_sweep_script)
- Optuna SQLite shared storage
- MlflowConfig env var generation
- project_name propagation to DAG for MLflow
- `--clean-logs` flag on `jernerics results`

## For future examples

When writing new examples for jernerics:

1. **Always `cd` into the example dir first.** This is the #1 mistake agents make.
2. **Use `path = "../.."` for local dev, `git` source for container builds.** Remember to switch back.
3. **Create a minimal README.md** if the container template includes it in %files.
4. **Test locally first** with `jernerics run local`, then dry-run, then real SLURM submission.
5. **The container build should be fast now** (~2-3 min) with `/dev/shm`. If it's slow, something is wrong — don't just bump the time limit.
6. **Run `uv lock --upgrade-package jernerics && uv sync`** after rebasing or pushing to the git branch, to update the pinned commit hash.
