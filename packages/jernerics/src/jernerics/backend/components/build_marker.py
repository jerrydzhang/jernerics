from pathlib import Path


def needs_rebuild(
    host,
    marker_path: str,
    local_lock_path: Path,
) -> bool:
    """Check if a rebuild is needed by comparing uv.lock mtime against a remote marker.

    Returns True if:
    - The marker file doesn't exist on the remote (first build or after clean)
    - The local uv.lock is newer than the remote marker
    - The local uv.lock doesn't exist (shouldn't happen, but treat as needing rebuild)
    """
    remote_mtime = host.getmtime(marker_path)
    if remote_mtime is None:
        return True

    if not local_lock_path.exists():
        return True

    return local_lock_path.stat().st_mtime > remote_mtime


def write_marker(host, marker_path: str) -> None:
    """Write a build marker file after a successful build."""
    host.write_file(marker_path, "")
