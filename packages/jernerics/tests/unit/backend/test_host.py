import subprocess
from pathlib import Path
from unittest.mock import patch

from jernerics.backend.host import LocalHost, SSHHost, StdoutHost


class TestLocalHostHome:
    def test_home_returns_path_home(self):
        host = LocalHost()
        assert host.home == str(Path.home())


class TestLocalHostShell:
    def test_shell_runs_sh_dash_c(self):
        host = LocalHost()
        with patch("jernerics.backend.host.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = host.shell("tar -C /cache -czf out.tar.gz tracking", check=False)

        mock_run.assert_called_once_with(
            ["sh", "-c", "tar -C /cache -czf out.tar.gz tracking"], check=False
        )
        assert result is mock_run.return_value

    def test_is_local(self):
        assert LocalHost().is_local is True


def _make_ssh_host():
    with patch("jernerics.backend.host.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="/home/user\n", stderr=""
        )
        return SSHHost("hpc1")


class TestSSHHostShell:
    def test_shell_hands_command_string_to_remote_shell(self):
        host = _make_ssh_host()
        with patch("jernerics.backend.host.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
            result = host.shell("tar -C /cache -czf out.tar.gz tracking", check=False)

        mock_run.assert_called_once_with(
            [
                "ssh",
                "-o",
                "LogLevel=ERROR",
                "hpc1",
                "tar -C /cache -czf out.tar.gz tracking",
            ],
            check=False,
        )
        assert result is mock_run.return_value

    def test_is_local(self):
        assert _make_ssh_host().is_local is False


class TestStdoutHostHome:
    def test_default_home_is_empty(self):
        host = StdoutHost()
        assert host.home == ""

    def test_custom_home(self):
        host = StdoutHost(home="/tmp")
        assert host.home == "/tmp"


class TestStdoutHostShell:
    def test_shell_returns_zero_without_touching_subprocess(self, capsys):
        host = StdoutHost()
        with patch("jernerics.backend.host.subprocess.run") as mock_run:
            result = host.shell("echo hi", check=False)

        mock_run.assert_not_called()
        assert result.returncode == 0
        assert result.args == ["sh", "-c", "echo hi"]
        assert result.stdout == ""
        assert result.stderr == ""

    def test_shell_prints_input(self, capsys):
        host = StdoutHost()
        host.shell("cat", input="payload")

        assert "payload" in capsys.readouterr().out

    def test_is_local(self):
        assert StdoutHost().is_local is False
