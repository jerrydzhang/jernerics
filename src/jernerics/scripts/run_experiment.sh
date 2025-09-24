#!/usr/bin/env bash

#SBATCH --partition=priority
#SBATCH --ntasks=1
#SBATCH --output=.cache/array_%A_%a.out
#SBATCH --error=.cache/array_%A_%a.err
#SBATCH --cpus-per-task=4
#SBATCH --mem=4G
#SBATCH --time=0

TRAIN_SCRIPT=$1
EXPERIMENT_CONFIG=$2
START_TIME=$3
RESULT_DIR=$4

python "$TRAIN_SCRIPT" "$EXPERIMENT_CONFIG" "$START_TIME" "$RESULT_DIR" "$SLURM_ARRAY_TASK_ID"
