#!/usr/bin/env bash
# Sync minimal files to HPC and build container there
# Uses Apptainer definition file + uv sync (deterministic via uv.lock)

set -e

HOST=${1:-"jez21005@hpc2.storrs.hpc.uconn.edu"}
REMOTE_DIR='~/experiments/container-gpu-test'
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)

echo "=== Syncing source to $HOST ==="

# Sync only what's needed to build:
# - jernerics source (dependency)
# - pyproject.toml + uv.lock (for uv sync)
# - container.def (build instructions)
# - dag.py, config.py, run.sh (experiment files)
echo "--- Syncing minimal build files ---"
tar czf - \
    -C "$PROJECT_ROOT" \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='.jernerics' \
    --exclude='result' \
    --exclude='*.tar.gz' \
    --exclude='*.sif' \
    --exclude='flake.lock' \
    --exclude='flake.*' \
    pyproject.toml \
    src/ \
    examples/container-gpu/pyproject.toml \
    examples/container-gpu/uv.lock \
    examples/container-gpu/container.def \
    examples/container-gpu/dag.py \
    examples/container-gpu/config.py \
    examples/container-gpu/run.sh \
    examples/container-gpu/src/ |
    ssh "$HOST" "rm -rf $REMOTE_DIR && mkdir -p $REMOTE_DIR && tar xzf - -C $REMOTE_DIR"

echo
echo "=== Submitting build job to SLURM ==="
ssh "$HOST" << 'ENDSSH'
cd ~/experiments/container-gpu-test/examples/container-gpu

# Create SLURM build job
cat > build_container.sh << 'BUILDSCRIPT'
#!/bin/bash
#SBATCH --job-name=container-build
#SBATCH --time=1:00:00
#SBATCH --mem=16G
#SBATCH --cpus-per-task=4
#SBATCH --output=build_%j.out
#SBATCH --error=build_%j.err

set -e

echo "=== Build started at $(date) ==="
echo "Running on $(hostname)"

cd ~/experiments/container-gpu-test/examples/container-gpu

echo
echo "--- Building container with Apptainer + uv sync ---"
echo "uv sync downloads from PyPI CDN (fast)"
time apptainer build --fakeroot container.sif container.def

echo
echo "--- Build result ---"
ls -lh container.sif

echo
echo "=== Build completed at $(date) ==="
BUILDSCRIPT

chmod +x build_container.sh
sbatch build_container.sh

echo
echo "Job submitted. Check status with:"
echo "  ssh $HOST 'squeue -u \$USER'"
echo "  ssh $HOST 'tail -f ~/experiments/container-gpu-test/examples/container-gpu/build_*.out'"
ENDSSH
