#!/usr/bin/env bash
# Sync minimal files to HPC and build container there
# Uses Apptainer definition file + uv sync (deterministic via uv.lock)
# jernerics dependency is pinned via git commit in pyproject.toml

set -e

HOST=${1:-"jez21005@hpc2.storrs.hpc.uconn.edu"}
REMOTE_DIR='~/experiments/container-gpu-test'
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)

echo "=== Syncing source to $HOST ==="

echo "--- Syncing project files ---"
tar czf - \
    -C "$SCRIPT_DIR" \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='*.sif' \
    pyproject.toml \
    uv.lock \
    container.def \
    dag.py \
    config.py \
    run.sh \
    src/ |
    ssh "$HOST" "mkdir -p $REMOTE_DIR && tar xzf - -C $REMOTE_DIR"

echo
echo "=== Submitting build job to SLURM ==="
ssh "$HOST" << 'ENDSSH'
cd ~/experiments/container-gpu-test

cat > build_container.sh << 'BUILDSCRIPT'
#!/bin/bash
#SBATCH --job-name=container-build
#SBATCH --partition=priority
#SBATCH --time=1:00:00
#SBATCH --mem=16G
#SBATCH --cpus-per-task=4
#SBATCH --output=build_%j.out
#SBATCH --error=build_%j.err

set -e

echo "=== Build started at $(date) ==="
echo "Running on $(hostname)"

cd ~/experiments/container-gpu-test

echo
echo "--- Building container with Apptainer + uv sync ---"
time apptainer build --fakeroot --force container.sif container.def

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
echo "  ssh $HOST 'tail -f ~/experiments/container-gpu-test/build_*.out'"
ENDSSH
