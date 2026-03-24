from unittest.mock import MagicMock, patch

from jernerics.hpc.ssh import SSHClient


class TestSSHClient:
    def test_init_sets_host(self):
        client = SSHClient("user@host.example.edu")
        assert client.host == "user@host.example.edu"

    def test_run_calls_ssh(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="output", returncode=0)
            result = client.run("ls -la")

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0] == ["ssh", "user@host.example.edu", "ls -la"]
            assert kwargs["capture_output"] is True
            assert kwargs["text"] is True

    def test_run_with_check_false(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="error", returncode=1)
            result = client.run("false", check=False)

            _args, kwargs = mock_run.call_args
            assert kwargs["check"] is False

    def test_run_with_timeout(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            result = client.run("long_command", timeout=60)

            _args, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 60

    def test_mkdir_calls_mkdir_p(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            client.mkdir("/path/to/dir")

            args, _kwargs = mock_run.call_args
            assert "mkdir -p /path/to/dir" in args[0][2]

    def test_file_exists_true(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = client.file_exists("/path/to/file.txt")

            assert result is True
            args, _kwargs = mock_run.call_args
            assert "test -f /path/to/file.txt" in args[0][2]

    def test_file_exists_false(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = client.file_exists("/nonexistent/file.txt")

            assert result is False

    def test_getmtime_success(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="1234567890\n", returncode=0)
            result = client.getmtime("/path/to/file.txt")

            assert result == 1234567890.0

    def test_getmtime_failure(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            result = client.getmtime("/nonexistent/file.txt")

            assert result is None

    def test_remove_file(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            client.remove_file("/path/to/file.txt")

            args, _kwargs = mock_run.call_args
            assert "rm -f /path/to/file.txt" in args[0][2]
