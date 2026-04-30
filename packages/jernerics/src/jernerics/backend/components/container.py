from collections.abc import Sequence
from typing import Protocol


class ContainerRuntime(Protocol):
    def wrap(self, command: str, binds: Sequence[str]) -> str: ...
    def build_command(self, project_dir: str) -> list[str]: ...
    def exists_command(self, project_dir: str) -> list[str]: ...


class NoContainer:
    def wrap(self, command: str, binds: Sequence[str]) -> str:
        _ = binds
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
    def wrap(
        self,
        command: str,
        binds: Sequence[str],
        *,
        gpu: bool = False,
    ) -> str:
        bind_args = []
        for bind in binds:
            bind_args.extend(["-v", bind])

        flags = ["--rm"]
        if gpu:
            flags.append("--gpus all")
        flags.extend(["-w", "/work"])

        return (
            f"docker run {' '.join(flags)} {' '.join(bind_args)}"
            f" container.sif {command}"
        )

    def build_command(self, project_dir: str) -> list[str]:
        return ["docker", "build", "-t", "container.sif", project_dir]

    def exists_command(self, project_dir: str) -> list[str]:
        _ = project_dir
        return ["docker", "image", "inspect", "container.sif"]
