from importlib import resources
from typing import Any, Dict, Tuple

import yaml


class NoExperimentsFound(Exception):
    """Exception raised when no experiments are found in the config file."""

    pass


def load_config(config_file: str) -> Tuple[Dict[str, Any], int]:
    """
    Loads experiment configuration from a YAML file.

    Args:
        config_file: Path to the configuration file.

    Returns:
        A tuple containing the config data and the number of experiments.

    Raises:
        NoExperimentsFound: If no experiments are found in the file.
    """
    with open(config_file, "r") as f:
        config_data = yaml.safe_load(f)

    num_experiments = len(config_data.get("experiments", []))

    if num_experiments == 0:
        raise NoExperimentsFound("No experiments found in the configuration file.")

    return config_data, num_experiments


def get_script_path(script_name: str, script_module: str = "jernerics.scripts") -> str:
    """
    Gets the path to a script within the package.

    Args:
        script_name: The name of the script file.
        script_module: The module where the script is located.

    Returns:
        The absolute path to the script.
    """
    return str(resources.files(script_module).joinpath(script_name))
