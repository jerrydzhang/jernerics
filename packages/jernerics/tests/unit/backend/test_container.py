from jernerics.backend.container import Apptainer, Docker, NoContainer


class TestDockerImageName:
    def test_uses_project_name_as_image(self):
        container = Docker(image_name="myproject")
        result = container.wrap("python run.py", ["src:/work"])
        assert "myproject" in result
        assert "container.sif" not in result

    def test_build_tags_with_project_name(self):
        container = Docker(image_name="myproject")
        cmd = container.build_command("/some/dir")
        assert "-t" in cmd
        idx = cmd.index("-t")
        assert cmd[idx + 1] == "myproject"

    def test_exists_checks_project_name(self):
        container = Docker(image_name="myproject")
        cmd = container.exists_command("/some/dir")
        assert "myproject" in cmd


class TestApptainerImageName:
    def test_uses_container_sif_regardless(self):
        container = Apptainer()
        result = container.wrap("python run.py", ["src:/work"])
        assert "container.sif" in result


class TestApptainerEnvPassthrough:
    def test_wrap_adds_env_flags(self):
        container = Apptainer()
        result = container.wrap(
            "python run.py",
            ["src:/work", "cache:/cache"],
            env={"FOO": "bar", "BAZ": "qux"},
        )
        assert "--env FOO=bar" in result
        assert "--env BAZ=qux" in result


class TestDockerEnvPassthrough:
    def test_wrap_adds_env_flags(self):
        container = Docker()
        result = container.wrap(
            "python run.py",
            ["src:/work", "cache:/cache"],
            env={"FOO": "bar", "BAZ": "qux"},
        )
        assert "-e FOO=bar" in result
        assert "-e BAZ=qux" in result


class TestNoContainerEnvPassthrough:
    def test_wrap_ignores_env(self):
        container = NoContainer()
        result = container.wrap(
            "python run.py",
            ["src:/work"],
            env={"FOO": "bar"},
        )
        assert result == "python run.py"
