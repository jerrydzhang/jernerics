import os
from urllib.parse import urlsplit


class TrackingServerSchemeError(ValueError):
    """Tracking server address lacks an http:// or https:// scheme."""


def resolve_tracking_ship(
    server_addr: str,
) -> tuple[str, str | None] | None:
    """Resolve the HTTP tracking server base URL and optional API key.

    server_addr is a full HTTP base URL (e.g. "http://homelab:8000").
    Returns (base_url, api_key) or None when server_addr is empty.
    Raises TrackingServerSchemeError for a scheme-less address (e.g. a
    v2-era "host:port" value) so every shipper fails at resolution
    instead of late inside httpx.
    """
    if not server_addr:
        return None
    if urlsplit(server_addr).scheme not in ("http", "https"):
        raise TrackingServerSchemeError(
            f"tracking server address {server_addr!r} is missing its "
            "http:// or https:// scheme. Set JERNERICS_TRACKING_SERVER "
            "or [tool.jernerics] tracking_server in pyproject.toml to a "
            f"full URL, for example https://{server_addr}"
        )
    return server_addr, os.environ.get("JERNERICS_API_KEY")
