from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from jernerics.cli import (
    _create_minimal_pyproject,
    _get_default_jernerics_config,
    _get_runner_code,
)


class TestGetRunnerCode:
    def test_basic_runner_code(self):
        code = _get_runner_code("/path/to/dag.py", "/path/to/config.py", 0)

        assert "/path/to/dag.py" in code
        assert "/path/to/config.py" in code
        assert "config_index" in code
        assert "DAG(dag_file)" in code
        assert "load_config" in code

    def test_runner_code_with_config_index(self):
        code = _get_runner_code("/dag.py", "/config.py", 5)

        assert '"5"' in code or "config_index" in code

    def test_runner_code_with_container(self):
        code = _get_runner_code(
            "/dag.py", "/config.py", 0, container_path="/container.sif"
        )

        assert "/container.sif" in code

    def test_runner_code_without_container(self):
        code = _get_runner_code("/dag.py", "/config.py", 0, container_path=None)

        assert "container_path = None" in code

    def test_runner_code_imports_dag(self):
        code = _get_runner_code("/dag.py", "/config.py", 0)

        assert "from jernerics.dag import DAG" in code

    def test_runner_code_handles_failure(self):
        code = _get_runner_code("/dag.py", "/config.py", 0)

        assert "isinstance(result, Exception)" in code
        assert "sys.exit(1)" in code


class TestGetDefaultJernericsConfig:
    def test_returns_dict_with_required_keys(self):
        config = _get_default_jernerics_config("myproject")

        assert "hpc" in config
        assert "container" in config
        assert "shell" in config

    def test_hpc_config_structure(self):
        config = _get_default_jernerics_config("myproject")

        assert "host" in config["hpc"]
        assert "remote_dir" in config["hpc"]
        assert "myproject" in config["hpc"]["remote_dir"]

    def test_container_config_structure(self):
        config = _get_default_jernerics_config("myproject")

        assert "partition" in config["container"]
        assert "time" in config["container"]
        assert "mem" in config["container"]
        assert "cpus" in config["container"]

    def test_shell_config_structure(self):
        config = _get_default_jernerics_config("myproject")

        assert "partition" in config["shell"]
        assert "cpus" in config["shell"]
        assert "mem" in config["shell"]
        assert "gpu" in config["shell"]

    def test_uses_project_name_in_remote_dir(self):
        config = _get_default_jernerics_config("test-project-123")

        assert "test-project-123" in config["hpc"]["remote_dir"]


class TestCreateMinimalPyproject:
    def test_returns_dict_with_required_keys(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert "project" in pyproject
        assert "tool" in pyproject
        assert "build-system" in pyproject

    def test_project_section_structure(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert pyproject["project"]["name"] == "myproject"
        assert "version" in pyproject["project"]
        assert "requires-python" in pyproject["project"]
        assert "jernerics" in pyproject["project"]["dependencies"]

    def test_tool_section_structure(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert "uv" in pyproject["tool"]
        assert "jernerics" in pyproject["tool"]
        assert "sources" in pyproject["tool"]["uv"]
        assert "jernerics" in pyproject["tool"]["uv"]["sources"]

    def test_build_system_structure(self):
        jernerics_config = _get_default_jernerics_config("myproject")
        pyproject = _create_minimal_pyproject("myproject", jernerics_config)

        assert "requires" in pyproject["build-system"]
        assert "build-backend" in pyproject["build-system"]


class TestDefaultSlurm:
    def test_default_slurm_values(self):
        from jernerics.cli import DEFAULT_SLURM

        assert "output" in DEFAULT_SLURM
        assert "error" in DEFAULT_SLURM
        assert "max_parallel" not in DEFAULT_SLURM
        assert "%A_%a" in DEFAULT_SLURM["output"]


class TestInitCommand:
    def test_init_creates_pyproject(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        assert (project_dir / "pyproject.toml").exists()

    def test_init_creates_container_def(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        assert (project_dir / "container.def").exists()

    def test_init_requires_uv(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = None

            from jernerics.cli import init

            with pytest.raises(SystemExit):
                init(str(project_dir))

    def test_init_invalid_template(self, tmp_path):
        project_dir = tmp_path / "new-project"

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"

            from jernerics.cli import init

            with pytest.raises(SystemExit):
                init(str(project_dir), template="nonexistent")

    def test_init_preserves_existing_container_def(self, tmp_path):
        project_dir = tmp_path / "existing-project"
        project_dir.mkdir()
        (project_dir / "container.def").write_text("existing definition")

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/usr/bin/uv"
            with patch("jernerics.cli.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

                from jernerics.cli import init

                init(str(project_dir))

        assert (project_dir / "container.def").read_text() == "existing definition"


class TestMainFunction:
    def test_main_calls_app(self):
        from jernerics.cli import main

        with patch("jernerics.cli.app") as mock_app:
            main()
            mock_app.assert_called_once()
