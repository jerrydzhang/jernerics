import sys

import pytest
from jernerics_server.__main__ import _is_loopback, main


class TestIsLoopback:
    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "[::1]", ""])
    def test_loopback_hosts(self, host):
        assert _is_loopback(host) is True

    @pytest.mark.parametrize("host", ["0.0.0.0", "[::]", "::", "192.168.1.5"])
    def test_non_loopback_hosts(self, host):
        assert _is_loopback(host) is False


@pytest.fixture
def serve_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "jernerics_server.__main__.serve",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    return calls


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["jernerics_server", *argv])
    monkeypatch.delenv("JERNERICS_API_KEY", raising=False)
    main()


class TestMainDefaults:
    def test_default_launch_binds_loopback_and_warns(
        self, monkeypatch, capsys, serve_calls
    ):
        _run(monkeypatch, [])
        assert len(serve_calls) == 1
        _, kwargs = serve_calls[0]
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["api_key"] is None
        err = capsys.readouterr().err
        assert "authentication is DISABLED" in err
        assert "loopback-only" in err
        assert "JERNERICS_API_KEY" in err

    def test_keyless_public_bind_fails_closed(self, monkeypatch, capsys, serve_calls):
        with pytest.raises(SystemExit) as excinfo:
            _run(monkeypatch, ["--host", "[::]"])
        assert excinfo.value.code == 1
        assert serve_calls == []
        err = capsys.readouterr().err
        assert "JERNERICS_API_KEY" in err
        assert "--allow-unauthenticated" in err
        assert "[::]" in err
        assert "Listening on" not in err

    def test_keyless_public_bind_with_flag_serves_with_strong_warning(
        self, monkeypatch, capsys, serve_calls
    ):
        _run(monkeypatch, ["--host", "0.0.0.0", "--allow-unauthenticated"])
        assert len(serve_calls) == 1
        _, kwargs = serve_calls[0]
        assert kwargs["host"] == "0.0.0.0"
        err = capsys.readouterr().err
        assert "authentication is DISABLED" in err
        assert "anyone who can reach this host" in err

    def test_keyed_public_bind_serves(self, monkeypatch, capsys, serve_calls):
        monkeypatch.setattr(sys, "argv", ["jernerics_server", "--host", "[::]"])
        monkeypatch.setenv("JERNERICS_API_KEY", "secret123")
        main()
        assert len(serve_calls) == 1
        _, kwargs = serve_calls[0]
        assert kwargs["host"] == "[::]"
        assert kwargs["api_key"] == "secret123"
        err = capsys.readouterr().err
        assert "API key authentication enabled" in err
        assert "DISABLED" not in err
