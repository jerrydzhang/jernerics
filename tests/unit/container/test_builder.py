from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from jernerics.container.builder import ContainerBuilder, _validate_slurm_value


class TestValidateSlurmValue:
    def test_valid_alphanumeric(self):
        assert _validate_slurm_value("abc123", "test") == "abc123"

    def test_valid_with_underscore(self):
        assert _validate_slurm_value("abc_123", "test") == "abc_123"

    def test_valid_with_hyphen(self):
        assert _validate_slurm_value("abc-123", "test") == "abc-123"

    def test_valid_with_period(self):
        assert _validate_slurm_value("abc.123", "test") == "abc.123"

    def test_valid_with_colon(self):
        assert _validate_slurm_value("1:00:00", "time") == "1:00:00"

    def test_valid_with_slash(self):
        assert _validate_slurm_value("/path/to/file", "path") == "/path/to/file"

    def test_invalid_with_space(self):
        with pytest.raises(ValueError, match="Invalid test value"):
            _validate_slurm_value("abc 123", "test")

    def test_invalid_with_special_char(self):
        with pytest.raises(ValueError, match="Invalid test value"):
            _validate_slurm_value("abc$123", "test")

    def test_invalid_with_shell_metachar(self):
        with pytest.raises(ValueError, match="Invalid test value"):
            _validate_slurm_value("abc;rm -rf", "test")


class TestContainerBuilderInit:
    def test_init_with_project_dir(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient") as mock_ssh,
            patch("jernerics.container.builder.FileSyncer") as mock_syncer,
            patch("jernerics.container.builder.SlurmJobManager") as mock_slurm,
        ):
            builder = ContainerBuilder(tmp_project)
            assert builder.project_dir == tmp_project
            mock_ssh.assert_called_once()

    def test_init_without_project_dir_finds_pyproject(self, tmp_project, monkeypatch):
        monkeypatch.chdir(tmp_project)
        with (
            patch("jernerics.container.builder.SSHClient") as mock_ssh,
            patch("jernerics.container.builder.FileSyncer") as mock_syncer,
            patch("jernerics.container.builder.SlurmJobManager") as mock_slurm,
        ):
            builder = ContainerBuilder()
            assert builder.project_dir == tmp_project

    def test_init_no_pyproject_raises(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match=r"No pyproject.toml found"):
            ContainerBuilder()

    def test_init_no_host_raises(self, tmp_path):
        project_dir = tmp_path / "no-host-project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "no-host-project"
version = "0.1.0"
""")
        with pytest.raises(ValueError, match="HPC host not configured"):
            ContainerBuilder(project_dir)


class TestGetRemoteDir:
    def test_basic_remote_dir(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)
            remote_dir = builder._get_remote_dir()
            assert remote_dir == "~/experiments/test-project"

    def test_remote_dir_without_trailing_slash(self, tmp_path):
        project_dir = tmp_path / "myproject"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "myproject"
version = "0.1.0"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
remote_dir = "~/experiments/{project_name}/"
""")
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(project_dir)
            remote_dir = builder._get_remote_dir()
            assert remote_dir == "~/experiments/myproject"

    def test_invalid_project_name_raises(self, tmp_path):
        project_dir = tmp_path / "invalid project!"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("""
[project]
name = "invalid-project"
version = "0.1.0"

[tool.jernerics.hpc]
host = "user@hpc.example.edu"
""")
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            with pytest.raises(ValueError, match="Invalid project name"):
                ContainerBuilder(project_dir)


class TestGenerateBuildScript:
    def test_script_contains_sbatch_directives(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)
            script = builder._generate_build_script("/home/user/logs")

            assert "#SBATCH --job-name=container-build" in script
            assert "#SBATCH --partition=priority" in script
            assert "#SBATCH --time=1:00:00" in script
            assert "#SBATCH --mem=16G" in script
            assert "#SBATCH --cpus-per-task=4" in script

    def test_script_contains_build_commands(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)
            script = builder._generate_build_script("/home/user/logs")

            assert "apptainer build" in script
            assert "container.sif" in script
            assert "container.def" in script

    def test_script_uses_output_dir(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)
            script = builder._generate_build_script("/custom/output/path")

            assert "/custom/output/path/build_%j.out" in script
            assert "/custom/output/path/build_%j.err" in script


class TestNeedsRebuild:
    def test_force_returns_true(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer") as mock_syncer,
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            mock_syncer.return_value.container_needs_rebuild.return_value = False
            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").write_text("version = 1\n")

            assert builder.needs_rebuild(force=True) is True

    def test_no_lock_raises(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").unlink(missing_ok=True)

            with pytest.raises(FileNotFoundError, match=r"uv.lock not found"):
                builder.needs_rebuild()

    def test_delegates_to_syncer(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer") as mock_syncer,
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            mock_instance = mock_syncer.return_value
            mock_instance.container_needs_rebuild.return_value = True

            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").write_text("version = 1\n")

            result = builder.needs_rebuild(force=False)
            assert result is True
            mock_instance.container_needs_rebuild.assert_called_once()


class TestEnsureContainerDef:
    def test_existing_def_returns_false(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)
            (tmp_project / "container.def").write_text("existing content")

            result = builder.ensure_container_def()
            assert result is False
            assert (tmp_project / "container.def").read_text() == "existing content"

    def test_creates_def_when_missing(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)

            result = builder.ensure_container_def()
            assert result is True
            content = (tmp_project / "container.def").read_text()
            assert "Bootstrap: docker" in content


class TestBuild:
    def test_build_no_lock_raises(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient"),
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").unlink(missing_ok=True)

            with pytest.raises(FileNotFoundError, match=r"uv.lock not found"):
                builder.build()

    def test_build_dry_run(self, tmp_project, capsys):
        with (
            patch("jernerics.container.builder.SSHClient") as mock_ssh,
            patch("jernerics.container.builder.FileSyncer"),
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            mock_ssh_instance = mock_ssh.return_value
            mock_ssh_instance.expand_tilde.return_value = (
                "/home/user/experiments/test-project"
            )

            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").write_text("version = 1\n")

            result = builder.build(dry_run=True)
            assert result is None

            captured = capsys.readouterr()
            assert "DRY RUN" in captured.out
            assert "Project dir:" in captured.out
            assert "Remote dir:" in captured.out

    def test_build_returns_none_when_up_to_date(self, tmp_project, capsys):
        with (
            patch("jernerics.container.builder.SSHClient") as mock_ssh,
            patch("jernerics.container.builder.FileSyncer") as mock_syncer,
            patch("jernerics.container.builder.SlurmJobManager"),
        ):
            mock_ssh_instance = mock_ssh.return_value
            mock_ssh_instance.expand_tilde.return_value = (
                "/home/user/experiments/test-project"
            )
            mock_syncer.return_value.container_needs_rebuild.return_value = False

            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").write_text("version = 1\n")

            result = builder.build(force=False, dry_run=False)
            assert result is None

            captured = capsys.readouterr()
            assert "Container is up to date" in captured.out

    def test_build_submits_job(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient") as mock_ssh,
            patch("jernerics.container.builder.FileSyncer") as mock_syncer,
            patch("jernerics.container.builder.SlurmJobManager") as mock_slurm,
            patch("jernerics.container.builder.subprocess.run") as mock_run,
        ):
            mock_ssh_instance = mock_ssh.return_value
            mock_ssh_instance.expand_tilde.return_value = (
                "/home/user/experiments/test-project"
            )

            mock_syncer_instance = mock_syncer.return_value
            mock_syncer_instance.container_needs_rebuild.return_value = True

            mock_slurm_instance = mock_slurm.return_value
            mock_slurm_instance.submit.return_value = "12345"

            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").write_text("version = 1\n")

            result = builder.build(force=True)
            assert result == "12345"

            mock_syncer_instance.sync_project.assert_called_once()
            mock_ssh_instance.mkdir.assert_called_once()
            mock_slurm_instance.submit.assert_called_once()

            meta_file = tmp_project / ".jernerics" / "jobs" / "12345.json"
            assert meta_file.exists()
            meta = json.loads(meta_file.read_text())
            assert meta["job_id"] == "12345"
            assert meta["job_type"] == "build"

    def test_build_upload_failure_raises(self, tmp_project):
        with (
            patch("jernerics.container.builder.SSHClient") as mock_ssh,
            patch("jernerics.container.builder.FileSyncer") as mock_syncer,
            patch("jernerics.container.builder.SlurmJobManager"),
            patch("jernerics.container.builder.subprocess.run") as mock_run,
        ):
            mock_ssh_instance = mock_ssh.return_value
            mock_ssh_instance.expand_tilde.return_value = (
                "/home/user/experiments/test-project"
            )

            mock_syncer_instance = mock_syncer.return_value
            mock_syncer_instance.container_needs_rebuild.return_value = True

            mock_run.return_value = MagicMock(
                returncode=1, stderr="SSH error", stdout=""
            )

            builder = ContainerBuilder(tmp_project)
            (tmp_project / "uv.lock").write_text("version = 1\n")

            with pytest.raises(RuntimeError, match="Failed to upload build script"):
                builder.build(force=True)
