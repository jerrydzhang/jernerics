#!/usr/bin/env python

# SBATCH --partition=priority
# SBATCH --ntasks=1
# SBATCH --output=.cache/array_%A_%a.out
# SBATCH --error=.cache/array_%A_%a.err
# SBATCH --mem=1G
# SBATCH --time=00:10:00

import json
import os
import sys


def combine_json_files(input_directory, output_filename):
    """
    Combines all JSON files in a directory into a single JSON file.

    Args:
        input_directory (str): The path to the directory containing JSON files.
        output_filename (str): The name of the output JSON file.
    """
    all_data = {}

    for filename in os.listdir(input_directory):
        if filename.endswith(".json"):
            file_path = os.path.join(input_directory, filename)
            try:
                with open(file_path, "r") as f:
                    data = json.load(f)
                    model_num = os.path.splitext(filename)[0].split("_")[0]
                    all_data[f"{model_num}_model"] = data
            except json.JSONDecodeError:
                print(f"Error decoding JSON from file: {filename}")
            except FileNotFoundError:
                print(f"File not found: {filename}")

    with open(output_filename, "w") as outfile:
        json.dump(all_data, outfile, indent=4)
        print(f"Successfully combined {len(all_data)} files into {output_filename}")


if __name__ == "__main__":
    if len(sys.argv) == 3:
        input_dir = sys.argv[1]
        output_file = sys.argv[2]
    else:
        print("Usage: python cleanup_experiment.py <input_directory> <output_file>")
        sys.exit(1)

    combine_json_files(input_dir, output_file)
