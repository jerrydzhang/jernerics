from collections.abc import Sequence
from typing import Protocol


class ContainerRuntime(Protocol):
    def wrap(
        self,
        command: str,
        binds: Sequence[str],
        *,
        env: dict[str, str] | None = None,
    ) -> str: ...

    def build_command(self, project_dir: str) -> list[str]: ...
    def exists_command(self, project_dir: str) -> list[str]: ...


class NoContainer:
    def wrap(
        self,
        command: str,
        binds: Sequence[str],
        *,
        env: dict[str, str] | None = None,
    ) -> str:
        _ = binds, env
        return command

    def build_command(self, project_dir: str) -> list[str]:
        _ = project_dir
        return []

    def exists_command(self, project_dir: str) -> list[str]:
        _ = project_dir
        return ["true"]


class Apptainer:
    def wrap(
        self,
        command: str,
        binds: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        fakeroot: bool = True,
        gpu: bool = True,
        contain: bool = True,
    ) -> str:
        flags = []
        if fakeroot:
            flags.append("--fakeroot")
        if contain:
            flags.append("--contain")
        if gpu:
            flags.append("--nv")
        if env:
            flags.extend(f"--env {k}={v}" for k, v in env.items())
        flags.append("--pwd /work")

        bind_str = " \\\n    --bind ".join(binds)

        return (
            f"apptainer exec {' '.join(flags)}"
            f" --bind {bind_str} container.sif {command}"
        )

    def build_command(self, project_dir: str) -> list[str]:
        _ = project_dir
        return [
            "apptainer",
            "build",
            "--fakeroot",
            "--force",
            "container.sif",
            "container.def",
        ]

    def exists_command(self, project_dir: str) -> list[str]:
        return ["test", "-f", f"{project_dir}/container.sif"]


class Docker:
    def __init__(self, image_name: str = "container.sif"):
        self.image_name = image_name

    def wrap(
        self,
        command: str,
        binds: Sequence[str],
        *,
        env: dict[str, str] | None = None,
        gpu: bool = False,
    ) -> str:
        bind_args = []
        for bind in binds:
            bind_args.extend(["-v", bind])

        flags = ["--rm", "--network=host"]
        if gpu:
            flags.append("--gpus all")
        if env:
            flags.extend(f"-e {k}={v}" for k, v in env.items())
        flags.extend(["-w", "/work"])

        return (
            f"docker run {' '.join(flags)} {' '.join(bind_args)}"
            f" {self.image_name} {command}"
        )

    def build_command(self, project_dir: str) -> list[str]:
        return ["docker", "build", "-t", self.image_name, project_dir]

    def exists_command(self, project_dir: str) -> list[str]:
        _ = project_dir
        return ["docker", "image", "inspect", self.image_name]
