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
        raise RuntimeError("Expected list of sweeps from server")  # noqa: TRY004
    return result


def list_trials(
    base_url: str,
    project: str,
    study_name: str,
    limit: int | None = None,
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
    if limit is not None:
        query_params["limit"] = str(limit)
    if metric_keys:
        query_params["metric_keys"] = metric_keys
    url = f"{base_url.rstrip('/')}/api/trials?{urlencode(query_params)}"
    result = _request(url)
    if not isinstance(result, list):
        raise RuntimeError("Expected list of trials from server")  # noqa: TRY004
    return result


def compare_sweeps(
    base_url: str,
    project: str,
    left: str,
    right: str,
    metrics: list[str] | None = None,
) -> dict:
    """Compare two sweeps from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        left: Left sweep/study name.
        right: Right sweep/study name.
        metrics: Optional list of metric keys to filter by.

    Returns:
        Comparison dictionary with trial counts, key overlap, and metric stats.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    query_params = {"project": project, "left": left, "right": right}
    if metrics is not None:
        query_params["metrics"] = ",".join(metrics)
    url = f"{base_url.rstrip('/')}/api/compare-sweeps?{urlencode(query_params)}"
    result = _request(url)
    if not isinstance(result, dict):
        raise RuntimeError("Expected comparison dict from server")  # noqa: TRY004
    return result


def get_metric_history(
    base_url: str, project: str, study_name: str, key: str
) -> list[dict]:
    """Get metric history from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        study_name: Study/sweep name.
        key: Metric key.

    Returns:
        List of metric history entries with trial_id, step, value, timestamp_ns.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    query_params = {"project": project, "study_name": study_name, "key": key}
    url = f"{base_url.rstrip('/')}/api/metrics?{urlencode(query_params)}"
    result = _request(url)
    if not isinstance(result, list):
        raise RuntimeError("Expected list of metric history entries from server")  # noqa: TRY004
    return result


def get_health(base_url: str) -> dict:
    """Check health of the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").

    Returns:
        Health dictionary (e.g., {"ok": True}).

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    url = f"{base_url.rstrip('/')}/api/health"
    result = _request(url)
    if not isinstance(result, dict):
        raise RuntimeError("Expected health dict from server")  # noqa: TRY004
    return result


def list_artifacts(
    base_url: str,
    project: str,
    study_name: str,
    trial_id: int | None = None,
) -> list[dict]:
    """List artifacts from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        study_name: Study/sweep name.
        trial_id: Optional trial ID to filter artifacts by.

    Returns:
        List of artifact dictionaries with trial_id, key, filename, timestamp_ns.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    query_params = {"project": project, "study_name": study_name}
    if trial_id is not None:
        query_params["trial_id"] = str(trial_id)
    url = f"{base_url.rstrip('/')}/api/artifacts?{urlencode(query_params)}"
    result = _request(url)
    if not isinstance(result, list):
        raise RuntimeError("Expected list of artifacts from server")  # noqa: TRY004
    return result


def list_results(
    base_url: str,
    project: str,
    study_name: str,
    trial_id: int | None = None,
    key: str | None = None,
) -> list[dict]:
    """List results from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        study_name: Study/sweep name.
        trial_id: Optional trial ID to filter results by.
        key: Optional result key to filter by.

    Returns:
        List of result dictionaries with trial_id, key, value, timestamp_ns.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    query_params = {"project": project, "study_name": study_name}
    if trial_id is not None:
        query_params["trial_id"] = str(trial_id)
    if key is not None:
        query_params["key"] = key
    url = f"{base_url.rstrip('/')}/api/results?{urlencode(query_params)}"
    result = _request(url)
    if not isinstance(result, list):
        raise RuntimeError("Expected list of results from server")  # noqa: TRY004
    return result


def list_params(
    base_url: str,
    project: str,
    study_name: str,
    trial_id: int | None = None,
    key: str | None = None,
) -> list[dict]:
    """List params from the tracking HTTP server.

    Args:
        base_url: Base URL of the tracking server (e.g., "http://localhost:8000").
        project: Project name.
        study_name: Study/sweep name.
        trial_id: Optional trial ID to filter params by.
        key: Optional param key to filter by.

    Returns:
        List of param dictionaries with trial_id, key, value, timestamp_ns.

    Raises:
        RuntimeError: If the server is unreachable, returns an error, or
            returns invalid JSON.
    """
    query_params = {"project": project, "study_name": study_name}
    if trial_id is not None:
        query_params["trial_id"] = str(trial_id)
    if key is not None:
        query_params["key"] = key
    url = f"{base_url.rstrip('/')}/api/params?{urlencode(query_params)}"
    result = _request(url)
    if not isinstance(result, list):
        raise RuntimeError("Expected list of params from server")  # noqa: TRY004
    return result
