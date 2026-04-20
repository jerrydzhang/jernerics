# Branch: optuna-mlflow-integration — TODO

Pre-merge checklist. Delete this file before merging to main.

## Must Do

- [ ] **Revert example pyproject.toml sources** — `examples/sweep-basic/pyproject.toml` (and any other examples touched during testing) should point back to the git branch, not local paths. Verify all examples still reference `git = "https://github.com/jerrydzhang/jernerics.git"` with `branch = "optuna-mlflow-integration"` (or remove branch pin after merge).

- [ ] **Add `mlflow` dependency to examples that need it** — `examples/sweep-basic/pyproject.toml` has `mlflow` added. Decide if other sweep examples (`sweep-parallel`, `no-objective-sweep`) should also list it, or if it's only needed when `[tool.jernerics.mlflow]` is configured.

- [ ] **Remove auto-mlflow metric scanning from runner code** — `_get_sweep_runner_code()` currently auto-logs every numeric field from every task result dict. Remove this loop. Users should call `mlflow.log_metric()` / `mlflow.log_artifact()` explicitly in their task code. Params from `search_space()` remain auto-logged (defined once in config, not per-task).

- [ ] **Local-first mlflow logging via FileStore** — During sweeps, the generated runner code should set `mlflow.set_tracking_uri("file:///mlruns")` (bind-mounted from `{cache_dir}/{project_name}/mlruns` on scratch). All logging goes to local filesystem on HPC scratch. No network dependency during the sweep. The `tracking_uri` in `[tool.jernerics.mlflow]` becomes the sync destination, not the live logging target.

- [ ] **Automatic sync after each trial** — After each trial's DAG execution completes, attempt to sync that run to the remote tracking server. This runs on the compute node (outbound HTTPS to home server via Tailscale Funnel or Cloudflare Tunnel). Best-effort — if it fails, data stays on scratch and can be synced later. Implemented as a function in the generated runner code, not a CLI command.

- [ ] **`jernerics mlflow sync` command** — User-facing CLI command that SSHes to HPC login node, reads the scratch FileStore, and pushes unsynced runs to the remote server. Idempotent (skips runs already on remote). Used for on-demand mid-sweep visibility or recovery from failed automatic syncs.

- [ ] **Move optuna DB to scratch** — The optuna SQLite DB is currently at `/work/.jernerics/optuna/` (home directory via bind mount). Move it to `{cache_dir}/{project_name}/optuna/` on scratch. Optuna study data is sweep-local — after the sweep, it's redundant with mlflow. No reason to consume home directory quota.

- [ ] **NixOS module for mlflow tracking server** — Add a NixOS module exposed via `nixosModules` in `flake.nix`. Options: `services.jernerics.mlflow.enable`, `.port` (5000), `.host` ("127.0.0.1"), `.backendStoreUri`, `.openFirewall` (false), `.basicAuth.enable`, `.basicAuth.adminUsername`, `.basicAuth.adminPasswordFile` (sops-nix compatible). Binds localhost by default — external access via Tailscale Funnel or Cloudflare Tunnel (separate from this module). Uses `mlflow-server` from nixpkgs with `--app-name basic-auth`.

- [ ] **`JERNERICS_MLFLOW_PASSWORD` on HPC** — The SLURM script does `export MLFLOW_TRACKING_PASSWORD=${JERNERICS_MLFLOW_PASSWORD}`. This needs to be set in the user's shell profile on the HPC login node (e.g., sourced from a file in `~/.config/jernerics/`). The NixOS module creates the admin user via `basic_auth.ini` with the password from sops-nix. Not a module concern — document the pattern.

## Nice to Have

- [ ] **Remove `run_local`'s duplicate study creation** — `run_local` creates the optuna study in the parent process, then the subprocess runner does `optuna.create_study(..., load_if_exists=True)` again. Works but is redundant now that the runner always creates with `load_if_exists`. Could simplify.
