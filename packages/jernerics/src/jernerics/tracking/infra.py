import os


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
