# Branch: optuna-mlflow-integration — TODO

Pre-merge checklist. Delete this file before merging to main.

## Must Do

- [x] **Revert example pyproject.toml sources** — `examples/sweep-basic/pyproject.toml` (and any other examples touched during testing) should point back to the git branch, not local paths. Verify all examples still reference `git = "https://github.com/jerrydzhang/jernerics.git"` with `branch = "optuna-mlflow-integration"` (or remove branch pin after merge).

- [x] **Add `mlflow` dependency to examples that need it** — `examples/sweep-basic/pyproject.toml` has `mlflow` added. Decide if other sweep examples (`sweep-parallel`, `no-objective-sweep`) should also list it, or if it's only needed when `[tool.jernerics.mlflow]` is configured.

- [x] **Remove auto-mlflow metric scanning from runner code** — `_get_sweep_runner_code()` currently auto-logs every numeric field from every task result dict. Remove this loop. Users should call `mlflow.log_metric()` / `mlflow.log_artifact()` explicitly in their task code. Params from `search_space()` remain auto-logged (defined once in config, not per-task).

- [x] **Local-first mlflow logging via FileStore** — During sweeps, the generated runner code sets `MLFLOW_TRACKING_URI=file:///scratch/mlruns` (bind-mounted from `{cache_dir}/{project_name}/mlruns` on scratch). All logging goes to local filesystem on HPC scratch. No network dependency during the sweep. The `tracking_uri` in `[tool.jernerics.mlflow]` becomes the sync destination, not the live logging target.

- [x] **Automatic sync after each trial** — After each trial's DAG execution completes, attempt to sync that run to the remote tracking server. This runs on the compute node (outbound HTTPS to home server via Tailscale Funnel or Cloudflare Tunnel). Best-effort — if it fails, data stays on scratch and can be synced later. Implemented as a function in the generated runner code, not a CLI command.

- [x] **`jernerics mlflow sync` command** — User-facing CLI command that SSHes to HPC login node, reads the scratch FileStore, and pushes unsynced runs to the remote server. Idempotent (skips runs already on remote). Used for on-demand mid-sweep visibility or recovery from failed automatic syncs.

- [x] **Move optuna DB to scratch** — Optuna SQLite DB moved to `{cache_dir}/{project_name}/optuna/` on scratch (bind-mounted at `/scratch/optuna` in container). Falls back to `/work/.jernerics/optuna/` when no `cache_dir` configured. Optuna study data is sweep-local — after the sweep, it's redundant with mlflow. No reason to consume home directory quota.

- [x] **NixOS module for mlflow tracking server** — Add a NixOS module exposed via `nixosModules` in `flake.nix`. Options: `services.jernerics.mlflow.enable`, `.port` (5000), `.host` ("127.0.0.1"), `.backendStoreUri`, `.openFirewall` (false), `.basicAuth.enable`, `.basicAuth.adminUsername`, `.basicAuth.adminPasswordFile` (sops-nix compatible). Binds localhost by default — external access via Tailscale Funnel or Cloudflare Tunnel (separate from this module). Uses `mlflow-server` from nixpkgs with `--app-name basic-auth`.

- [x] **`JERNERICS_MLFLOW_PASSWORD` on HPC** — The SLURM script does `export MLFLOW_TRACKING_PASSWORD=${JERNERICS_MLFLOW_PASSWORD}`. This needs to be set in the user's shell profile on the HPC login node (e.g., sourced from a file in `~/.config/jernerics/`). The NixOS module creates the admin user via `basic_auth.ini` with the password from sops-nix. Not a module concern — document the pattern.

## Nice to Have

- [x] **Remove `run_local`'s duplicate study creation** — `run_local` creates the optuna study in the parent process, then the subprocess runner does `optuna.create_study(..., load_if_exists=True)` again. Works but is redundant now that the runner always creates with `load_if_exists`. Could simplify.

## Pre-merge audit (2026-04-21)

### Must fix

- [ ] **Failing test: `test_remote_dir_no_double_slashes`** — `JERNERICS_MLFLOW_TRACKING_URI` leaks from shell into tests via env var. Either unset it in test setup or extend the `sqlite:///` stripping to also handle `https://`/`http://` prefixes.

- [ ] **README documents deleted API** — `merge_configs` and old `configs = [...]` format shown in Quick Start but both are gone. Config format section needs full rewrite to `_base`/`search_space`/`n_trials`. CLI reference missing `jernerics mlflow sync`. Environment variables missing `JERNERICS_MLFLOW_*`. Example names outdated (removed `container-basic`/`container-gpu`, added new ones).

- [ ] **Tests still use deleted `configs = [...]` syntax** — 10 occurrences across `test_cli_run.py` (8), `test_provenance.py` (1), `conftest.py` (1). They work by accident (`load_config` silently ignores `configs`). Should be updated to new format or at minimum verified that they're testing the right thing.

- [ ] **Delete this file before merging** — Per the header instruction.

### Should fix

- [ ] **`mlflow-export-import` as hard dependency** — Only used by `mlflow sync` and auto-sync. Users who don't use mlflow tracking still must install it and its transitive deps. Consider an optional dependency group.

### Code smell / tech debt

- [ ] **`expand_path` in `_generate_sweep_script` expands `~` locally** — `Path(p).expanduser()` resolves to the *local* home dir, but the result goes into `#SBATCH --output/error` directives that run on the remote. If local and remote homes differ, paths will be wrong. AGENTS.md explicitly warns against this. `ContainerBuilder` does it correctly via `ssh.expand_tilde()`.

- [ ] **`_get_mlflow_sync_script` accepts `username` param but never uses it** — Dead parameter. The generated script relies on env vars for auth.

- [ ] **Duplicated `mlflowWithUI` derivation in `flake.nix`** — Identical block copied 3 times (apps, packages, modules/mlflow.nix). Should be a shared `let` binding.

- [ ] **`_get_runner_code` passes `container_path`, `_get_sweep_runner_code` doesn't** — Minor inconsistency if these helpers are ever called directly.

- [ ] **`load_config` silently ignores old `configs = [...]`** — No error or warning for users upgrading from the old format. They get `_base={}` silently.
