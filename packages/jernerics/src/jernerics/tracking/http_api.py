import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _request(url: str) -> list[dict] | dict:
    """Internal helper to make HTTP requests with error handling.

    Args:
        url: Full URL to request.

    Returns:
        Parsed JSON response.

    Raises:
        RuntimeError: On HTTPError, URLError, or invalid JSON.
    """
    api_key = os.environ.get("JERNERICS_API_KEY")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = Request(url, headers=headers)

    try:
        with urlopen(req) as response:
            data = response.read().decode("utf-8")
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                raise RuntimeError("Server returned invalid JSON") from None
    except HTTPError as e:
        body = e.read().decode("utf-8")
        error_detail = None
        try:
            error_json = json.loads(body)
            error_detail = error_json.get("detail") or error_json.get("error")
        except json.JSONDecodeError:
            pass
        if error_detail:
            raise RuntimeError(f"HTTP {e.code}: {error_detail}") from None
        raise RuntimeError(f"HTTP {e.code}") from None
    except URLError as e:
        base_url = url.split("?")[0]
        raise RuntimeError(
            f"Cannot reach tracking server at {base_url}: {e.reason}"
        ) from None


def list_sweeps(base_url: str, project: str | None = None) -> list[dict]:
    """List sweeps from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Optional project name to filter sweeps by.

    Returns:
        List of sweep dictionaries.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    url = f"{base_url.rstrip('/')}/api/sweeps"
    if project is not None:
        query_params = urlencode({"project": project})
        url += f"?{query_params}"
    result = _request(url)
    if not isinstance(result, list):
        raise TypeError("Expected list of sweeps from server")
    return result


def list_trials(
    base_url: str,
    project: str,
    study_name: str,
    limit: int = 100,
    metric_keys: str | None = None,
) -> list[dict]:
    """List trials from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        study_name: Study/sweep name.
        limit: Maximum number of trials to return.
        metric_keys: Optional comma-separated list of metric keys to filter by.

    Returns:
        List of trial dictionaries.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    query_params = {"project": project, "study_name": study_name}
    if metric_keys:
        query_params["metric_keys"] = metric_keys
    url = f"{base_url.rstrip('/')}/api/trials?{urlencode(query_params)}"
    result = _request(url)
    if not isinstance(result, list):
        raise TypeError("Expected list of trials from server")
    if limit:
        result = result[:limit]
    return result


def compare_sweeps(base_url: str, project: str, left: str, right: str) -> dict:
    """Compare two sweeps from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        left: Left sweep/study name.
        right: Right sweep/study name.

    Returns:
        Comparison dictionary with trial counts, key overlap, and metric stats.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    query_params = urlencode({"project": project, "left": left, "right": right})
    url = f"{base_url.rstrip('/')}/api/compare-sweeps?{query_params}"
    result = _request(url)
    if not isinstance(result, dict):
        raise TypeError("Expected comparison dict from server")
    return result
