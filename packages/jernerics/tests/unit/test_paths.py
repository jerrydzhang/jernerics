from pathlib import Path

from jernerics.paths import cache_dir


class TestCacheDir:
    def test_in_container_resolves_cache_mount(self, monkeypatch):
        monkeypatch.setenv("JERNERICS_HPC", "1")
        assert cache_dir() == Path("/cache")

    def test_on_host_resolves_home_cache(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JERNERICS_HPC", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        assert cache_dir() == tmp_path / ".cache" / "jernerics"
