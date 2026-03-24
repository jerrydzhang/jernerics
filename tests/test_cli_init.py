import subprocess
import tomllib


class TestInitHelp:
    def test_shows_template_option(self):
        result = subprocess.run(
            ["jernerics", "init", "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "--template" in result.stdout


class TestInitCreatesProject:
    def test_creates_required_files(self, tmp_path):
        project_dir = tmp_path / "test-project"
        result = subprocess.run(
            ["jernerics", "init", str(project_dir)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert (project_dir / "pyproject.toml").exists()
        assert (project_dir / "container.def").exists()
        assert not (project_dir / "dag.py").exists()
        assert not (project_dir / "config.py").exists()

    def test_creates_src_directory(self, tmp_path):
        project_dir = tmp_path / "test-project"
        result = subprocess.run(
            ["jernerics", "init", str(project_dir)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert (project_dir / "src" / "__init__.py").exists()

    def test_includes_shell_config(self, tmp_path):
        project_dir = tmp_path / "test-project"
        result = subprocess.run(
            ["jernerics", "init", str(project_dir)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        with open(project_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        assert "shell" in data["tool"]["jernerics"]
        assert "partition" in data["tool"]["jernerics"]["shell"]
        assert "cpus" in data["tool"]["jernerics"]["shell"]

    def test_runs_uv_sync(self, tmp_path):
        project_dir = tmp_path / "test-project"
        result = subprocess.run(
            ["jernerics", "init", str(project_dir)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert (project_dir / "uv.lock").exists()


class TestInitExistingPyproject:
    def test_merges_jernerics_config(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text(
            """
[project]
name = "existing-project"
version = "1.0.0"
dependencies = ["numpy"]

[tool.other]
setting = "value"
"""
        )

        result = subprocess.run(
            ["jernerics", "init", str(project_dir), "--force"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

        with open(project_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)

        assert data["project"]["name"] == "existing-project"
        assert "numpy" in data["project"]["dependencies"]
        assert data["tool"]["other"]["setting"] == "value"
        assert "jernerics" in data["tool"]
        assert "hpc" in data["tool"]["jernerics"]

    def test_preserves_existing_container_def(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        (project_dir / "container.def").write_text("existing definition")

        result = subprocess.run(
            ["jernerics", "init", str(project_dir)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert (project_dir / "container.def").read_text() == "existing definition"

    def test_preserves_existing_src(self, tmp_path):
        project_dir = tmp_path / "test-project"
        project_dir.mkdir()
        src_dir = project_dir / "src"
        src_dir.mkdir()
        (src_dir / "mymodule.py").write_text("# my module")

        result = subprocess.run(
            ["jernerics", "init", str(project_dir)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert (src_dir / "mymodule.py").read_text() == "# my module"


class TestInitInvalidTemplate:
    def test_rejects_unknown_template(self, tmp_path):
        project_dir = tmp_path / "test-project"
        result = subprocess.run(
            ["jernerics", "init", str(project_dir), "--template", "nonexistent"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "Unknown template" in result.stdout
