#!/usr/bin/env bash

DAG_FILE=$1
CONFIG_FILE=$2
RESULTS_DIR=$3

if [[ -z "$SLURM_ARRAY_TASK_ID" ]]; then
    echo "Error: SLURM_ARRAY_TASK_ID must be set (this script is meant to run via sbatch --array)" >&2
    exit 1
fi

CONFIG_INDEX=$((SLURM_ARRAY_TASK_ID - 1))

cd "$(dirname "$DAG_FILE")"

export JERNERICS_DAG_FILE="$DAG_FILE"
export JERNERICS_CONFIG_FILE="$CONFIG_FILE"
export JERNERICS_RESULTS_DIR="$RESULTS_DIR"
export JERNERICS_CONFIG_INDEX="$CONFIG_INDEX"

python -c '
import os
import sys
import pathlib

dag_file = os.environ["JERNERICS_DAG_FILE"]
config_file = os.environ["JERNERICS_CONFIG_FILE"]
config_index = int(os.environ["JERNERICS_CONFIG_INDEX"])

sys.path.insert(0, str(pathlib.Path(dag_file).parent))

from jernerics.dag import DAG
from jernerics._cli_helpers import load_config

dag = DAG(dag_file)
slurm_opts, configs = load_config(config_file)
config = configs[config_index]

results = dag.run(config, config_index=config_index)

failed = [name for name, result in results.items() if isinstance(result, Exception)]
if failed:
    print("DAG failed. Tasks with errors:", ", ".join(failed))
    sys.exit(1)
else:
    print("DAG completed")
'
