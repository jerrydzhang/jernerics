---
## Goal

Create a CLI-driven workflow for running experiments on HPC (SLURM + Apptainer) with minimal per-project setup, agent-safe operations, and reproducible builds.

## Instructions

- Read the full design plan: `.opencode/hpc-cli-plan.md`
- No external container registries - build on HPC only
- All SSH operations scoped to configured remote_dir
- No compute on login node - always use SLURM
- Template selection is via `jernerics init --template` (one-time decision)
- User-provided `container.def` in project root is respected if it exists

## Completed

**Phase 1 - Container Commands:** `jernerics container build`
**Phase 2 - Run Commands:** `jernerics run slurm`, `jobs`, `cancel`, `logs`, `results`
**Phase 3 - Shell & Cleanup:** `jernerics shell`, `jernerics clean`
**Phase 4 - Polish:** All tasks complete

### Phase 4 Summary:
- **Init refactor:** No longer creates dag.py/config.py (these are not singletons); supports merging into existing pyproject.toml; prompts before overwriting `[tool.jernerics]`
- **Exit codes:** Added `ExitCode` enum (SUCCESS=0, GENERAL_ERROR=1, SSH_ERROR=2, CONFIG_ERROR=3, SLURM_ERROR=4, CONTAINER_ERROR=5)
- **Error messages:** Improved with suggested actions (e.g., "Run 'jernerics init' to create one")
- **JSON output:** Added `--json` flag to `jobs` command
- **TTY detection:** Added `is_tty()` helper for future progress indicators
- **Progress indicators:** Added step-by-step progress for `container build` and `run slurm`

## Relevant Files

```
.opencode/hpc-cli-plan.md          # Full design document

src/jernerics/
├── cli.py                         # All CLI commands
├── _cli_helpers.py                # Config loading, HpcConfig, ShellConfig, ExitCode, is_tty()
├── container/
│   ├── builder.py                 # Container build logic
│   └── templates.py               # Template loading
├── hpc/
│   ├── ssh.py                     # SSH operations
│   ├── slurm.py                   # SLURM job management
│   └── sync.py                    # File sync
└── templates/
    └── python.def                 # Default container template

tests/
├── test_cli_init.py               # Init command tests
├── test_cli_container.py          # Container build tests
├── test_cli_run_slurm.py          # Run slurm tests
├── test_cli_jobs.py               # Jobs/cancel/logs/results tests
├── test_cli_shell_clean.py        # Shell and clean tests
└── test_config_and_templates.py   # Config parsing and template tests
```
