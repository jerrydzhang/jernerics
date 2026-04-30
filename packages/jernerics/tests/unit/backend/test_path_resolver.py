from jernerics.backend.components.container import Apptainer
from jernerics.backend.components.path_resolver import PathResolver


def _resolver(**overrides):
    defaults = {
        "remote_dir": "~/projects/proj",
        "cache_dir": None,
        "container": Apptainer(),
        "work_mount_source": None,
        "quote_binds": False,
        "build_dir": None,
    }
    defaults.update(overrides)
    return PathResolver(**defaults)


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
