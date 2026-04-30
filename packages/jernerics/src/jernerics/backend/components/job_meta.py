import json
from pathlib import Path


def save_job_meta(
    *,
    job_id: str,
    remote_dir: str,
    n_trials: int,
    local_cache_dir: Path,
    backend: str | None = None,
    output_pattern: str | None = None,
    error_pattern: str | None = None,
) -> None:
    job_meta: dict = {
        "job_id": job_id,
        "remote_dir": remote_dir,
        "n_trials": n_trials,
    }
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
