#!/usr/bin/env bash
set -euo pipefail

CONTAINER=${1:-.jernerics/container.tar.gz}
DAG_FILE=${2:-dag.py}
CONFIG_FILE=${3:-config.py}

CONTAINER_ABS=$(cd "$(dirname "$CONTAINER")" && pwd)/$(basename "$CONTAINER")
SIF_PATH="${CONTAINER_ABS%.tar.gz}.sif"

if [[ ! -f "$SIF_PATH" ]]; then
    echo "Converting tarball to SIF..."
    if [[ "$CONTAINER_ABS" == *.tar.gz ]]; then
        gunzip -c "$CONTAINER_ABS" > /tmp/container-$$.tar
        apptainer build --force "$SIF_PATH" docker-archive:///tmp/container-$$.tar
        rm /tmp/container-$$.tar
    else
        apptainer build --force "$SIF_PATH" docker-archive://"$CONTAINER_ABS"
    fi
    echo "Created: $SIF_PATH"
fi

apptainer exec --bind "$PWD:/work" "$SIF_PATH" \
    jernerics run slurm --print-script --container "$SIF_PATH" --bind-dir "$PWD" "/work/$DAG_FILE" "/work/$CONFIG_FILE" | sbatch
