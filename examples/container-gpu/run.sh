#!/usr/bin/env bash
set -euo pipefail

CONTAINER=${1:-container.sif}
DAG_FILE=${2:-dag.py}
CONFIG_FILE=${3:-config.py}

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
CONTAINER_ABS="$SCRIPT_DIR/$CONTAINER"

if [[ ! -f "$CONTAINER_ABS" ]]; then
    echo "Container not found: $CONTAINER_ABS"
    echo "Build it with: ./test_sync_run.sh"
    exit 1
fi

cd "$SCRIPT_DIR"

apptainer exec --nv --bind "$SCRIPT_DIR:/work" "$CONTAINER_ABS" \
    jernerics run slurm --print-script --container "$CONTAINER_ABS" --bind-dir "$SCRIPT_DIR" "/work/$DAG_FILE" "/work/$CONFIG_FILE" | sbatch
