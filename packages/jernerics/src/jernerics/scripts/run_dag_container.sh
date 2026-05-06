#!/usr/bin/env bash
set -euo pipefail

CONTAINER=$1
DAG_FILE=$2
CONFIG_FILE=$3
RESULTS_DIR=$4

if [[ -n "$SLURM_ARRAY_TASK_ID" ]]; then
    CONFIG_INDEX=$((SLURM_ARRAY_TASK_ID - 1))
else
    CONFIG_INDEX=${5:-0}
fi

PROJECT_DIR=$(dirname "$DAG_FILE")
DAG_BASENAME=$(basename "$DAG_FILE")
CONFIG_BASENAME=$(basename "$CONFIG_FILE")

export JERNERICS_DAG_FILE="/work/$DAG_BASENAME"
export JERNERICS_CONFIG_FILE="/work/$CONFIG_BASENAME"
export JERNERICS_RESULTS_DIR="/work/$RESULTS_DIR"
export JERNERICS_CONFIG_INDEX="$CONFIG_INDEX"

apptainer exec \
    --fakeroot \
    --nv \
    --bind "$PROJECT_DIR:/work" \
    "$CONTAINER" \
    python -c '
import os
import sys
import pathlib

dag_file = os.environ["JERNERICS_DAG_FILE"]
config_file = os.environ["JERNERICS_CONFIG_FILE"]
config_index = int(os.environ["JERNERICS_CONFIG_INDEX"])

sys.path.insert(0, str(pathlib.Path(dag_file).parent))

from jernerics.dag import DAG
from jernerics.config import load_config

dag = DAG(dag_file)
slurm_opts, configs, max_workers = load_config(config_file)
config = configs[config_index]

results = dag.run(config, config_index=config_index, max_workers=max_workers)

failed = [name for name, result in results.items() if isinstance(result, Exception)]
if failed:
    print("DAG failed. Tasks with errors:", ", ".join(failed))
    sys.exit(1)
else:
    print("DAG completed")
'
