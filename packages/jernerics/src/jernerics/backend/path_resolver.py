from jernerics.backend.container import NoContainer
from jernerics.paths import CACHE_MOUNT

_PROJECT_NAME_TEMPLATE = "{project_name}"
_PROJECT_NAME_HYPHEN_TEMPLATE = "{project-name}"


def has_project_template(path: str) -> bool:
    return _PROJECT_NAME_TEMPLATE in path or _PROJECT_NAME_HYPHEN_TEMPLATE in path


def substitute_project_name(path: str, project_name: str) -> str:
    return path.replace(_PROJECT_NAME_TEMPLATE, project_name).replace(
        _PROJECT_NAME_HYPHEN_TEMPLATE, project_name
    )


def strip_project_template(path: str) -> str:
    if _PROJECT_NAME_TEMPLATE in path:
        return path.replace(f"/{_PROJECT_NAME_TEMPLATE}", "").replace(
            _PROJECT_NAME_TEMPLATE, ""
        )
    if _PROJECT_NAME_HYPHEN_TEMPLATE in path:
        return path.replace(f"/{_PROJECT_NAME_HYPHEN_TEMPLATE}", "").replace(
            _PROJECT_NAME_HYPHEN_TEMPLATE, ""
        )
    return path


class PathResolver:
    def __init__(
        self,
        remote_dir: str,
        cache_dir: str | None,
        container,
        *,
        work_mount_source: str | None = None,
        quote_binds: bool = False,
        build_dir: str | None = None,
        project_name: str = "",
    ):
        self.remote_dir = remote_dir
        self.cache_dir = cache_dir
        self.container = container
        self._work_mount_source = work_mount_source
        self._quote_binds = quote_binds
        self._build_dir = build_dir
        self._project_name = project_name

    @property
    def work_prefix(self) -> str:
        if isinstance(self.container, NoContainer):
            return self.remote_dir
        return "/work"

    @property
    def cache_prefix(self) -> str:
        if isinstance(self.container, NoContainer):
            return self.resolve_cache()
        return CACHE_MOUNT

    def storage_path(self, study_name: str) -> str:
        base = self.cache_prefix
        return f"{base}/optuna/{study_name}.journal"

    def retry_ctx_path(self, study_name: str) -> str:
        base = self.cache_prefix
        return f"{base}/retry/{study_name}_ctx.json"

    def tracking_dir(self, study_name: str) -> str:
        base = self.cache_prefix
        return f"{base}/tracking/{study_name}"

    def events_dir(self, study_name: str) -> str:
        return f"{self.tracking_dir(study_name)}/events"

    def artifacts_dir(self, study_name: str) -> str:
        return f"{self.tracking_dir(study_name)}/artifacts"

    def heartbeats_dir(self, study_name: str) -> str:
        return f"{self.tracking_dir(study_name)}/heartbeats"

    def resolve_cache(self, project_name: str = "") -> str:
        name = project_name or self._project_name
        cache = self.cache_dir or "/home/user/.cache/jernerics"
        if has_project_template(cache):
            cache = substitute_project_name(cache, name)
        elif name:
            cache = f"{cache}/{name}"
        return cache

    def bind_args(self, cache_host: str) -> list[str]:
        work_src = self._work_mount_source or self.remote_dir
        if self._quote_binds:
            return [f'"{work_src}:/work"', f'"{cache_host}:{CACHE_MOUNT}"']
        return [f"{work_src}:/work", f"{cache_host}:{CACHE_MOUNT}"]

    def resolve_build_dir(self, project_name: str) -> str | None:
        if self._build_dir is None:
            return None
        build_dir = self._build_dir
        if has_project_template(build_dir):
            return substitute_project_name(build_dir, project_name)
        if project_name:
            return f"{build_dir}/{project_name}"
        return build_dir
