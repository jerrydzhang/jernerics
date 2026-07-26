import hashlib
from pathlib import Path


def needs_rebuild(
    host,
    marker_path: str,
    local_lock_path: Path,
    container_def_path: Path | None = None,
) -> bool:
    """Check if a rebuild is needed by comparing uv.lock mtime and container.def hash.

    Returns True if:
    - The marker file doesn't exist on the remote (first build or after clean)
    - The local uv.lock is newer than the remote marker
    - The local uv.lock doesn't exist (shouldn't happen, but treat as needing rebuild)
    - The container definition hash differs from the stored marker hash
    """
    remote_mtime = host.getmtime(marker_path)
    if remote_mtime is None:
        return True

    if not local_lock_path.exists():
        return True

    if local_lock_path.stat().st_mtime > remote_mtime:
        return True

    if container_def_path is not None and container_def_path.exists():
        remote_marker = host.read_file(marker_path)
        current_hash = hashlib.sha256(container_def_path.read_bytes()).hexdigest()
        if (remote_marker or "").strip() != current_hash:
            return True

    return False


def write_marker(
    host,
    marker_path: str,
    container_def_path: Path | None = None,
) -> None:
    """Write a build marker file after a successful build.

    If ``container_def_path`` is provided and exists, writes its sha256 hash to
    the marker so future builds can detect definition changes. Otherwise writes
    an empty string (backward compatible).
    """
    if container_def_path is not None and container_def_path.exists():
        content = hashlib.sha256(container_def_path.read_bytes()).hexdigest()
    else:
        content = ""
    host.write_file(marker_path, content)
