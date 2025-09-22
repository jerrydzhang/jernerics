import importlib
import os
import pathlib
import sys
import time

import yaml


def deep_merge(dict1, dict2):
    """
    Recursively merges dict2 into dict1.
    """
    for key, value in dict2.items():
        if key in dict1 and isinstance(dict1[key], dict) and isinstance(value, dict):
            # If both values are dictionaries, recurse
            deep_merge(dict1[key], value)
        else:
            # Otherwise, just update or add the key-value pair
            dict1[key] = value
    return dict1


def main():
    print("Starting experiment...")

    config_file = sys.argv[1]
    timestamp = int(sys.argv[2])
    results_dir = sys.argv[3]
    task_id = int(sys.argv[4])

    # Add project root to path regardless of where script is called from
    # We search for a pyproject.toml file upwards from the config file
    p = pathlib.Path(config_file).resolve().parent
    while p != p.parent and not (p / "pyproject.toml").exists():
        p = p.parent
    # If we found it, add to path. Otherwise, we rely on CWD being in path
    if (p / "pyproject.toml").exists():
        sys.path.insert(0, str(p))

    # 1. Load params
    with open(config_file, "r") as f:
        all_config = yaml.safe_load(f)

    current_config = deep_merge(
        all_config.get("shared", {}),
        all_config["experiments"][task_id - 1],
    )
    current_config["name"] = all_config["name"]
    current_config["description"] = all_config.get("description", "")
    current_config["task_id"] = task_id
    current_config["timestamp"] = time.strftime(
        "%Y%m%d-%H%M%S", time.localtime(timestamp)
    )
    current_config["results_dir"] = pathlib.Path(results_dir)

    # 2. Get the experiment object
    experiment_module = importlib.import_module(current_config["module"])
    current_config.pop("module")
    experiment = experiment_module.get_experiment(current_config)

    experiment.run()


if __name__ == "__main__":
    main()
