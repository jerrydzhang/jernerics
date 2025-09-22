#!/usr/bin/env bash

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd -P)

EXPERIMENT_CONFIG=$1
START_TIME=$(date +%s)

# sbatch --array=0-9 "$SCRIPT_DIR/run_experiment.sh" "$EXPERIMENT_CONFIG" "$START_TIME"
"$SCRIPT_DIR/run_experiment.sh" "$EXPERIMENT_CONFIG" "$START_TIME"
