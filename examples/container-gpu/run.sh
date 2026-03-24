#!/usr/bin/env bash
set -euo pipefail

# Container built with: apptainer build --fakeroot container.sif container.def
CONTAINER=${1:-container.sif}
DAG_FILE=${2:-dag.py}
CONFIG_FILE=${3:-config.py}

CONTAINER_ABS=$(cd "$(dirname "$CONTAINER")" && pwd)/$(basename "$CONTAINER")

if [[ ! -f "$CONTAINER_ABS" ]]; then
    echo "Container not found: $CONTAINER_ABS"
    echo "Build it with: apptainer build --fakeroot container.sif container.def"
    exit 1
fi

apptainer exec --nv --bind "$PWD:/work" "$CONTAINER_ABS" \
    jernerics run slurm --print-script --container "$CONTAINER_ABS" --bind-dir "$PWD" "/work/$DAG_FILE" "/work/$CONFIG_FILE" | sbatch
