from pathlib import Path

from jernerics.backend.host import LocalHost, StdoutHost


class TestLocalHostHome:
    def test_home_returns_path_home(self):
        host = LocalHost()
        assert host.home == str(Path.home())


class TestStdoutHostHome:
    def test_default_home_is_empty(self):
        host = StdoutHost()
        assert host.home == ""

    def test_custom_home(self):
        host = StdoutHost(home="/tmp")
        assert host.home == "/tmp"
