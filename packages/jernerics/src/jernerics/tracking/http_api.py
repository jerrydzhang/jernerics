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
