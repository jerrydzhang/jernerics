import json
import os
from urllib.request import Request, urlopen


def list_sweeps(base_url: str) -> list[dict]:
    """List sweeps from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").

    Returns:
        List of sweep dictionaries.

    Raises:
        URLError: If the server is unreachable.
        HTTPError: If the server returns an error status.
        ValueError: If the response is not valid JSON.
    """
    api_key = os.environ.get("JERNERICS_API_KEY")
    url = f"{base_url.rstrip('/')}/api/sweeps"

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = Request(url, headers=headers)

    with urlopen(req) as response:
        data = response.read().decode("utf-8")
        return json.loads(data)


def list_trials(
    base_url: str, project: str, study_name: str, limit: int = 100
) -> list[dict]:
    """List trials from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        study_name: Study/sweep name.
        limit: Maximum number of trials to return.

    Returns:
        List of trial dictionaries.

    Raises:
        URLError: If the server is unreachable.
        HTTPError: If the server returns an error status.
        ValueError: If the response is not valid JSON.
    """
    api_key = os.environ.get("JERNERICS_API_KEY")
    url = f"{base_url.rstrip('/')}/api/trials?project={project}&study_name={study_name}"

    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = Request(url, headers=headers)

    with urlopen(req) as response:
        data = response.read().decode("utf-8")
        trials = json.loads(data)
        if limit:
            trials = trials[:limit]
        return trials
