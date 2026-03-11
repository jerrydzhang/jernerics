import subprocess
import sys


class TestCLI:
    def test_cli_help(self):
        result = subprocess.run(
            ["jernerics", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "run" in result.stdout

    def test_cli_run_help(self):
        result = subprocess.run(
            ["jernerics", "run", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "local" in result.stdout
        assert "slurm" in result.stdout

    def test_cli_run_local_help(self):
        result = subprocess.run(
            ["jernerics", "run", "local", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "DAG_FILE" in result.stdout
        assert "CONFIG_FILE" in result.stdout

    def test_cli_run_slurm_help(self):
        result = subprocess.run(
            ["jernerics", "run", "slurm", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--set" in result.stdout or "-S" in result.stdout
