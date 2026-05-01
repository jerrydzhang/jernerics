from pathlib import Path

from jernerics.backend.components.container import NoContainer

_PROJECT_NAME_TEMPLATE = "{project_name}"
_PROJECT_NAME_HYPHEN_TEMPLATE = "{project-name}"


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
        return "/cache"

    def storage_path(self, study_name: str) -> str:
        base = self.cache_prefix
        return f"{base}/optuna/{study_name}.journal"

    def retry_ctx_path(self, study_name: str) -> str:
        base = self.cache_prefix
        return f"{base}/retry/{study_name}_ctx.json"

    def tracking_dir(self, study_name: str) -> str:
        base = self.cache_prefix
        return f"{base}/tracking/{study_name}"

    def retry_host_path(self, cache_host: str, study_name: str) -> str:
        """Host-side path for the retry context file."""
        if isinstance(self.container, NoContainer):
            cache_host = cache_host.replace("$HOME", str(Path.home()))
        return f"{cache_host}/retry/{study_name}_ctx.json"

    def expand_path(self, path: str) -> str:
        """Expand $HOME in path for NoContainer."""
        if isinstance(self.container, NoContainer):
            return path.replace("$HOME", str(Path.home()))
        return path

    def resolve_cache(self, project_name: str = "") -> str:
        name = project_name or self._project_name
        cache = self.cache_dir or "$HOME/.cache/jernerics"
        if _PROJECT_NAME_TEMPLATE in cache:
            cache = cache.replace(_PROJECT_NAME_TEMPLATE, name)
        elif _PROJECT_NAME_HYPHEN_TEMPLATE in cache:
            cache = cache.replace(_PROJECT_NAME_HYPHEN_TEMPLATE, name)
        elif name:
            cache = f"{cache}/{name}"
        return cache.replace("~", "$HOME")

    def bind_args(self, cache_host: str) -> list[str]:
        work_src = self._work_mount_source or self.remote_dir
        if self._quote_binds:
            return [f'"{work_src}:/work"', f'"{cache_host}:/cache"']
        return [f"{work_src}:/work", f"{cache_host}:/cache"]

    def resolve_build_dir(self, project_name: str) -> str | None:
        if self._build_dir is None:
            return None
        build_dir = self._build_dir
        if _PROJECT_NAME_TEMPLATE in build_dir:
            build_dir = build_dir.replace(_PROJECT_NAME_TEMPLATE, project_name)
        elif _PROJECT_NAME_HYPHEN_TEMPLATE in build_dir:
            build_dir = build_dir.replace(_PROJECT_NAME_HYPHEN_TEMPLATE, project_name)
        elif project_name:
            build_dir = f"{build_dir}/{project_name}"
        return build_dir.replace("~", "$HOME")
