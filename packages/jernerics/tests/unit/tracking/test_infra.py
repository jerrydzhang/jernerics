import pytest
from jernerics.tracking.infra import (
    TrackingServerSchemeError,
    resolve_tracking_ship,
)


class TestResolveTrackingShip:
    def test_http_url_passes_through(self, monkeypatch):
        monkeypatch.delenv("JERNERICS_API_KEY", raising=False)
        assert resolve_tracking_ship("http://homelab:8000") == (
            "http://homelab:8000",
            None,
        )

    def test_https_url_passes_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("JERNERICS_API_KEY", "secret")
        assert resolve_tracking_ship("https://atlas.example:443") == (
            "https://atlas.example:443",
            "secret",
        )

    def test_empty_address_means_unconfigured(self):
        assert resolve_tracking_ship("") is None

    def test_scheme_less_address_names_env_var_pyproject_key_and_value(self):
        with pytest.raises(TrackingServerSchemeError) as excinfo:
            resolve_tracking_ship("atlas.taile454b.ts.net:443")
        message = str(excinfo.value)
        assert "atlas.taile454b.ts.net:443" in message
        assert "JERNERICS_TRACKING_SERVER" in message
        assert "[tool.jernerics] tracking_server" in message
        assert "https://atlas.taile454b.ts.net:443" in message

    def test_bare_host_port_raises(self):
        with pytest.raises(TrackingServerSchemeError, match="localhost:8000"):
            resolve_tracking_ship("localhost:8000")

    def test_non_http_scheme_raises(self):
        with pytest.raises(TrackingServerSchemeError, match="ftp://host"):
            resolve_tracking_ship("ftp://host")
