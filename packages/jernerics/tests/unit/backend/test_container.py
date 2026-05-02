from jernerics.backend.container import Apptainer, Docker, NoContainer


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
