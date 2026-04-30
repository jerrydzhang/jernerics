from collections.abc import Sequence
from typing import Protocol


class ContainerRuntime(Protocol):
    def wrap(self, command: str, binds: Sequence[str]) -> str: ...
    def exists(self, project_dir: str) -> bool: ...
    def build(self, project_dir: str) -> str | None: ...


class NoContainer:
    def wrap(self, command: str, binds: Sequence[str]) -> str:
        _ = binds  # Unused
        return command

    def exists(self, project_dir: str) -> bool:
        _ = project_dir  # Unused
        return True

    def build(self, project_dir: str) -> str | None:
        _ = project_dir  # Unused
        return None


class Apptainer:
    def __init__(self, host):
        self.host = host

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

    def exists(self, project_dir: str) -> bool:
        return self.host.file_exists(f"{project_dir}/container.sif")

    def build(self, project_dir: str) -> str | None:
        result = self.host.run(
            [
                "apptainer",
                "build",
                "--fakeroot",
                "--force",
                "container.sif",
                "container.def",
            ],
            cwd=project_dir,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Apptainer build failed: {result.stderr}")
        return "container.sif"


class Docker:
    def __init__(self, host):
        self.host = host

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

    def exists(self, project_dir: str) -> bool:
        _ = project_dir  # Unused
        result = self.host.run(
            ["docker", "image", "inspect", "container.sif"],
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def build(self, project_dir: str) -> str | None:
        result = self.host.run(
            ["docker", "build", "-t", "container.sif", project_dir],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Docker build failed: {result.stderr}")
        return "container.sif"
