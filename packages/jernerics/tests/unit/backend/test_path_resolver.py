from jernerics.backend.components.container import Apptainer, NoContainer
from jernerics.backend.components.path_resolver import PathResolver


def _resolver(**overrides):
    defaults = {
        "remote_dir": "~/projects/proj",
        "cache_dir": None,
        "container": Apptainer(),
        "work_mount_source": None,
        "quote_binds": False,
        "build_dir": None,
        "project_name": "",
    }
    defaults.update(overrides)
    return PathResolver(**defaults)


class TestCachePrefix:
    def test_no_container_includes_project_name(self):
        r = _resolver(
            container=NoContainer(),
            cache_dir="/home/user/.cache/jernerics",
            project_name="sweep-retry",
        )
        assert r.cache_prefix == "/home/user/.cache/jernerics/sweep-retry"

    def test_no_container_default_cache_dir(self):
        r = _resolver(container=NoContainer(), project_name="proj")
        assert r.cache_prefix == "$HOME/.cache/jernerics/proj"

    def test_no_container_no_project_name(self):
        r = _resolver(container=NoContainer(), cache_dir="/tmp/cache")
        assert r.cache_prefix == "/tmp/cache"

    def test_apptainer_returns_cache(self):
        r = _resolver(container=Apptainer(), project_name="proj")
        assert r.cache_prefix == "/cache"


class TestTrackingDir:
    def test_no_container_uses_project_name(self):
        r = _resolver(
            container=NoContainer(),
            cache_dir="/home/user/.cache/jernerics",
            project_name="proj",
        )
        expected = "/home/user/.cache/jernerics/proj/tracking/study"
        assert r.tracking_dir("study") == expected

    def test_apptainer_returns_cache_tracking(self):
        r = _resolver(container=Apptainer(), project_name="proj")
        assert r.tracking_dir("study") == "/cache/tracking/study"


class TestRetryCtxPath:
    def test_no_container_uses_project_name(self):
        r = _resolver(
            container=NoContainer(),
            cache_dir="/home/user/.cache/jernerics",
            project_name="proj",
        )
        expected = "/home/user/.cache/jernerics/proj/retry/study_ctx.json"
        assert r.retry_ctx_path("study") == expected

    def test_apptainer_returns_cache_retry(self):
        r = _resolver(container=Apptainer(), project_name="proj")
        assert r.retry_ctx_path("study") == "/cache/retry/study_ctx.json"


class TestStoragePath:
    def test_no_container_uses_project_name(self):
        r = _resolver(
            container=NoContainer(),
            cache_dir="/home/user/.cache/jernerics",
            project_name="proj",
        )
        expected = "/home/user/.cache/jernerics/proj/optuna/study.journal"
        assert r.storage_path("study") == expected

    def test_apptainer_returns_cache_optuna(self):
        r = _resolver(container=Apptainer(), project_name="proj")
        assert r.storage_path("study") == "/cache/optuna/study.journal"


class TestExpandPath:
    def test_no_container_expands_home(self):
        r = _resolver(container=NoContainer())
        result = r.expand_path("$HOME/.cache/jernerics/proj/optuna/study.journal")
        assert "$HOME" not in result
        assert "/" in result

    def test_apptainer_returns_unchanged(self):
        r = _resolver(container=Apptainer())
        path = "/cache/optuna/study.journal"
        assert r.expand_path(path) == path

    def test_no_home_literal_returns_unchanged(self):
        r = _resolver(container=NoContainer())
        path = "/tmp/cache/study.journal"
        assert r.expand_path(path) == path


class TestResolveBuildDir:
    def test_template_expansion(self):
        r = _resolver(build_dir="/dev/shm/build/{project_name}")
        assert r.resolve_build_dir("my-project") == "/dev/shm/build/my-project"

    def test_hyphen_template_expansion(self):
        r = _resolver(build_dir="/dev/shm/build/{project-name}")
        assert r.resolve_build_dir("my-project") == "/dev/shm/build/my-project"

    def test_no_template_appends_project_name(self):
        r = _resolver(build_dir="/dev/shm/build")
        assert r.resolve_build_dir("my-project") == "/dev/shm/build/my-project"

    def test_none_returns_none(self):
        r = _resolver(build_dir=None)
        assert r.resolve_build_dir("my-project") is None

    def test_tilde_replaced_with_home(self):
        r = _resolver(build_dir="~/build/{project_name}")
        assert r.resolve_build_dir("my-project") == "$HOME/build/my-project"
