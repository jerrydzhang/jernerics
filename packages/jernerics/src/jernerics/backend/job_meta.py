import json
from pathlib import Path


def save_job_meta(
    *,
    job_id: str,
    remote_dir: str,
    n_trials: int,
    local_cache_dir: Path,
    study_name: str | None = None,
    backend: str | None = None,
    output_pattern: str | None = None,
    error_pattern: str | None = None,
) -> None:
    job_meta: dict = {
        "job_id": job_id,
        "remote_dir": remote_dir,
        "n_trials": n_trials,
    }
    if study_name is not None:
        job_meta["study_name"] = study_name
    if backend is not None:
        job_meta["backend"] = backend
    if output_pattern is not None:
        job_meta["output_pattern"] = output_pattern
    if error_pattern is not None:
        job_meta["error_pattern"] = error_pattern

    meta_dir = local_cache_dir / "jobs"
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_file = meta_dir / f"{job_id}.json"
    meta_file.write_text(json.dumps(job_meta, indent=2))


def load_job_studies(local_cache_dir: Path) -> dict[str, str]:
    meta_dir = local_cache_dir / "jobs"
    if not meta_dir.is_dir():
        return {}
    studies: dict[str, str] = {}
    for meta_file in meta_dir.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        job_id = meta.get("job_id")
        study_name = meta.get("study_name")
        if job_id and study_name:
            studies[job_id] = study_name
    return studies


def load_job_backends(local_cache_dir: Path) -> dict[str, str]:
    """Scheduler type per job id from metadata; jobs without one are absent."""
    meta_dir = local_cache_dir / "jobs"
    if not meta_dir.is_dir():
        return {}
    backends: dict[str, str] = {}
    for meta_file in meta_dir.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text())
        except (OSError, ValueError):
            continue
        job_id = meta.get("job_id")
        backend = meta.get("backend")
        if job_id and backend:
            backends[str(job_id)] = str(backend)
    return backends
