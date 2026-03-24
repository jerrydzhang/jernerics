from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from jernerics._cli_helpers import ExitCode
from jernerics.cli import app

runner = CliRunner()


class TestShellHelp:
    def test_shows_options(self):
        result = runner.invoke(app, ["shell", "--help"])
        assert result.exit_code == 0
        assert "--gpu" in result.stdout
        assert "--cpus" in result.stdout
        assert "--mem" in result.stdout
        assert "--time" in result.stdout
        assert "--partition" in result.stdout
        assert "--no-container" in result.stdout


class TestShellRequiresConfig:
    def test_fails_without_pyproject_toml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["shell"])
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "No pyproject.toml found" in result.stdout

    def test_fails_without_hpc_host(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "test-project"
"""
        )
        result = runner.invoke(app, ["shell"])
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "No HPC host configured" in result.stdout


class TestShellCommand:
    def test_uses_config_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"

[tool.jernerics.shell]
partition = "gpu"
cpus = 4
mem = "8G"
gpu = 2
time = "2:00:00"
""")

        with patch("jernerics.cli.subprocess.run") as mock_run:
            with patch("jernerics.cli.FileSyncer") as mock_syncer:
                mock_syncer.return_value.container_exists.return_value = False
                result = runner.invoke(app, ["shell"])

        assert result.exit_code == 0
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "ssh"
        assert call_args[1] == "-t"
        assert call_args[2] == "user@hpc.example.edu"
        srun_cmd = call_args[3]
        assert "--partition" in srun_cmd
        assert "gpu" in srun_cmd
        assert "--cpus-per-task" in srun_cmd
        assert "4" in srun_cmd
        assert "--mem" in srun_cmd
        assert "8G" in srun_cmd
        assert "--time" in srun_cmd
        assert "2:00:00" in srun_cmd
        assert "--gres" in srun_cmd
        assert "gpu:2" in srun_cmd

    def test_cli_overrides_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"

[tool.jernerics.shell]
partition = "default"
cpus = 1
mem = "4G"
gpu = 0
""")

        with patch("jernerics.cli.subprocess.run") as mock_run:
            with patch("jernerics.cli.FileSyncer") as mock_syncer:
                mock_syncer.return_value.container_exists.return_value = False
                result = runner.invoke(
                    app,
                    [
                        "shell",
                        "--gpu",
                        "1",
                        "--cpus",
                        "8",
                        "--mem",
                        "16G",
                        "--time",
                        "4:00:00",
                        "--partition",
                        "priority",
                    ],
                )

        assert result.exit_code == 0
        call_args = mock_run.call_args[0][0]
        srun_cmd = call_args[3]
        assert "gpu:1" in srun_cmd
        assert "8" in srun_cmd
        assert "16G" in srun_cmd
        assert "4:00:00" in srun_cmd
        assert "priority" in srun_cmd

    def test_enters_container_if_exists(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")

        with patch("jernerics.cli.subprocess.run") as mock_run:
            with patch("jernerics.cli.FileSyncer") as mock_syncer:
                mock_syncer.return_value.container_exists.return_value = True
                result = runner.invoke(app, ["shell"])

        assert result.exit_code == 0
        call_args = mock_run.call_args[0][0]
        srun_cmd = call_args[3]
        assert "apptainer exec" in srun_cmd
        assert "container.sif" in srun_cmd

    def test_no_container_flag_skips_container(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")

        with patch("jernerics.cli.subprocess.run") as mock_run:
            with patch("jernerics.cli.FileSyncer") as mock_syncer:
                mock_syncer.return_value.container_exists.return_value = True
                result = runner.invoke(app, ["shell", "--no-container"])

        assert result.exit_code == 0
        call_args = mock_run.call_args[0][0]
        srun_cmd = call_args[3]
        assert "apptainer" not in srun_cmd


class TestCleanHelp:
    def test_shows_options(self):
        result = runner.invoke(app, ["clean", "--help"])
        assert result.exit_code == 0
        assert "--results" in result.stdout
        assert "--logs" in result.stdout
        assert "--container" in result.stdout
        assert "--all" in result.stdout
        assert "--force" in result.stdout


class TestCleanRequiresConfig:
    def test_fails_without_pyproject_toml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["clean"])
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "No pyproject.toml found" in result.stdout

    def test_fails_without_hpc_host(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "test-project"
"""
        )
        result = runner.invoke(app, ["clean"])
        assert result.exit_code == ExitCode.CONFIG_ERROR
        assert "No HPC host configured" in result.stdout


class TestCleanDryRun:
    def test_dry_run_shows_what_would_delete(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")

        result = runner.invoke(app, ["clean", "--results", "--logs"])

        assert result.exit_code == 0
        assert "Would delete" in result.stdout
        assert "results/" in result.stdout
        assert ".jernerics/logs/" in result.stdout
        assert "Dry run" in result.stdout

    def test_no_targets_shows_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text(
            """
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
"""
        )

        result = runner.invoke(app, ["clean"])

        assert result.exit_code == ExitCode.GENERAL_ERROR
        assert "Nothing to clean" in result.stdout

    def test_all_flag_includes_everything(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")

        result = runner.invoke(app, ["clean", "--all"])

        assert result.exit_code == 0
        assert "results/" in result.stdout
        assert ".jernerics/logs/" in result.stdout
        assert "container.sif" in result.stdout


class TestCleanWithForce:
    def test_force_deletes_files(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")

        with patch("jernerics.cli.SSHClient") as mock_ssh:
            mock_ssh.return_value.run.return_value = MagicMock(returncode=0)
            result = runner.invoke(app, ["clean", "--results", "--force"])

        assert result.exit_code == 0
        assert "Deleted:" in result.stdout

    def test_force_reports_failures(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "pyproject.toml").write_text("""
[project]
name = "test-project"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")

        with patch("jernerics.cli.SSHClient") as mock_ssh:
            mock_ssh.return_value.run.return_value = MagicMock(
                returncode=1, stderr="Permission denied"
            )
            result = runner.invoke(app, ["clean", "--results", "--force"])

        assert result.exit_code == 0
        assert "Failed to delete" in result.stdout
