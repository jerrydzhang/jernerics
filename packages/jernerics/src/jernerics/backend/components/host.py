import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol


class Host(Protocol):
    def run(self, command: Sequence[str], **kwargs) -> subprocess.CompletedProcess: ...
    def mkdir(self, remote_path: str) -> None: ...
    def file_exists(self, remote_path: str) -> bool: ...
    def getmtime(self, remote_path: str) -> float | None: ...
    def remove_file(self, remote_path: str) -> None: ...
    def write_file(self, remote_path: str, content: str) -> None: ...


class LocalHost:
    def run(self, command: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(command, **kwargs)

    def mkdir(self, remote_path: str) -> None:
        Path(remote_path).mkdir(parents=True, exist_ok=True)

    def file_exists(self, remote_path: str) -> bool:
        return Path(remote_path).is_file()

    def getmtime(self, remote_path: str) -> float | None:
        path = Path(remote_path)
        if path.is_file():
            return path.stat().st_mtime
        return None

    def remove_file(self, remote_path: str) -> None:
        path = Path(remote_path)
        if path.is_file():
            path.unlink()

    def write_file(self, remote_path: str, content: str) -> None:
        Path(remote_path).write_text(content)


class StdoutHost:
    """Prints commands to stdout instead of executing them.

    Used by retry_checker running inside a container — the composed bash
    script is piped to bash on the login node via stdout.
    """

    def run(self, command: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        input_text = kwargs.get("input", "")
        if input_text:
            print(input_text)
        return subprocess.CompletedProcess(
            args=list(command), returncode=0, stdout="", stderr=""
        )

    def mkdir(self, remote_path: str) -> None:
        pass

    def file_exists(self, remote_path: str) -> bool:
        return False

    def getmtime(self, remote_path: str) -> float | None:
        return None

    def remove_file(self, remote_path: str) -> None:
        pass

    def write_file(self, remote_path: str, content: str) -> None:
        pass


class SSHHost(Host):
    def __init__(self, host: str):
        self.host = host

    def run(self, command: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", "-o", "LogLevel=ERROR", self.host] + list(command), **kwargs
        )

    def mkdir(self, remote_path: str) -> None:
        self.run(["mkdir", "-p", remote_path], check=True)

    def file_exists(self, remote_path: str) -> bool:
        result = self.run(["test", "-f", remote_path], check=False)
        return result.returncode == 0

    def getmtime(self, remote_path: str) -> float | None:
        result = self.run(
            ["stat", "-c", "%Y", remote_path],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
        return None

    def remove_file(self, remote_path: str) -> None:
        self.run(["rm", "-f", remote_path], check=True)

    def write_file(self, remote_path: str, content: str) -> None:
        self.run(
            [f"cat > {remote_path}"],
            input=content,
            text=True,
            check=True,
        )
