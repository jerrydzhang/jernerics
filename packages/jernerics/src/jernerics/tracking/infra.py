import os
from typing import Any

import httpx


def resolve_tracking_ship(
    server_addr: str,
) -> tuple[str, str | None] | None:
    """Resolve the HTTP tracking server base URL and optional API key.

    server_addr is a full HTTP base URL (e.g. "http://homelab:8000").
    Returns (base_url, api_key) or None when server_addr is empty.
    """
    if not server_addr:
        return None
    return server_addr, os.environ.get("JERNERICS_API_KEY")


def resolve_artifact_storage(base_url: str | None = None) -> Any:
    """Resolve an upload function shipping artifact files to the HTTP server.

    base_url is the tracking server URL (the same one events ship to). When
    omitted, falls back to the JERNERICS_TRACKING_URL env var. Returns
    upload_file(key, local_path) that POSTs the file to
    {base_url}/artifact/{key}, or None when no server is configured.
    """
    if not base_url:
        base_url = os.environ.get("JERNERICS_TRACKING_URL")
    if not base_url:
        return None
    base_url = base_url.rstrip("/")
    api_key = os.environ.get("JERNERICS_API_KEY")
    headers = {"authorization": f"Bearer {api_key}"} if api_key else None

    def upload_file(key: str, local_path: str) -> None:
        with open(local_path, "rb") as f:
            httpx.post(
                f"{base_url}/artifact/{key}",
                content=f,
                headers=headers,
                timeout=None,
            )

    return upload_file
