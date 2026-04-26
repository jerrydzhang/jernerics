from unittest.mock import MagicMock, patch

import pytest
from jernerics.hpc.ssh import SSHClient, _quote_path, _validate_path


class TestValidatePath:
    def test_valid_path(self):
        assert _validate_path("/path/to/file") == "/path/to/file"

    def test_null_bytes_raises(self):
        with pytest.raises(ValueError, match="Path cannot contain null bytes"):
            _validate_path("/path/with\x00null")


class TestQuotePath:
    def test_quotes_path_with_spaces(self):
        result = _quote_path("/path/with spaces/file.txt")
        assert result == "'/path/with spaces/file.txt'"

    def test_preserves_tilde(self):
        result = _quote_path("~/path/to/file")
        assert result.startswith("~")

    def test_quotes_special_chars_after_tilde(self):
        result = _quote_path("~/path with spaces/file")
        assert result == "~'/path with spaces/file'"

    def test_quotes_path_with_dollar(self):
        result = _quote_path("/path/$VAR/file")
        assert "$VAR" not in result or "'" in result


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
            assert args[0] == [
                "ssh",
                "-o",
                "LogLevel=ERROR",
                "user@host.example.edu",
                "ls -la",
            ]
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

    def test_run_with_input(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            client.run("cat", input="hello world")

            _args, kwargs = mock_run.call_args
            assert kwargs.get("input") == "hello world"

    def test_run_script(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="script output", returncode=0)
            result = client.run_script("echo hello")

            mock_run.assert_called_once()
            args, kwargs = mock_run.call_args
            assert args[0] == [
                "ssh",
                "-o",
                "LogLevel=ERROR",
                "user@host.example.edu",
                "bash -s",
            ]
            assert kwargs["input"] == "echo hello"
            assert kwargs["capture_output"] is True
            assert kwargs["text"] is True

    def test_run_script_with_timeout(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            client.run_script("long script", timeout=120)

            _args, kwargs = mock_run.call_args
            assert kwargs["timeout"] == 120

    def test_mkdir_calls_mkdir_p(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            client.mkdir("/path/to/dir")

            args, _kwargs = mock_run.call_args
            assert "mkdir -p /path/to/dir" in args[0][4]

    def test_mkdir_null_bytes_raises(self):
        client = SSHClient("user@host.example.edu")

        with pytest.raises(ValueError, match="Path cannot contain null bytes"):
            client.mkdir("/path/with\x00null")

    def test_file_exists_true(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = client.file_exists("/path/to/file.txt")

            assert result is True
            args, _kwargs = mock_run.call_args
            assert "test -f /path/to/file.txt" in args[0][4]

    def test_file_exists_false(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = client.file_exists("/nonexistent/file.txt")

            assert result is False

    def test_file_exists_null_bytes_raises(self):
        client = SSHClient("user@host.example.edu")

        with pytest.raises(ValueError, match="Path cannot contain null bytes"):
            client.file_exists("/path/with\x00null")

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

    def test_getmtime_null_bytes_raises(self):
        client = SSHClient("user@host.example.edu")

        with pytest.raises(ValueError, match="Path cannot contain null bytes"):
            client.getmtime("/path/with\x00null")

    def test_remove_file(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            client.remove_file("/path/to/file.txt")

            args, _kwargs = mock_run.call_args
            assert "rm -f /path/to/file.txt" in args[0][4]

    def test_remove_file_null_bytes_raises(self):
        client = SSHClient("user@host.example.edu")

        with pytest.raises(ValueError, match="Path cannot contain null bytes"):
            client.remove_file("/path/with\x00null")

    def test_get_home_dir(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="/home/username\n", returncode=0)
            result = client.get_home_dir()

            assert result == "/home/username"
            args, kwargs = mock_run.call_args
            assert "echo $HOME" in args[0][4]
            assert kwargs["check"] is True

    def test_expand_tilde_with_tilde(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="/home/username\n", returncode=0)
            result = client.expand_tilde("~/projects/test")

            assert result == "/home/username/projects/test"

    def test_expand_tilde_without_tilde(self):
        client = SSHClient("user@host.example.edu")

        with patch("jernerics.hpc.ssh.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="/home/username\n", returncode=0)
            result = client.expand_tilde("/absolute/path/test")

            assert result == "/absolute/path/test"
