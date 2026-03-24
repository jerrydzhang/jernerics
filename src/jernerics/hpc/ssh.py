import shlex
import subprocess


def _validate_path(path: str) -> str:
    if "\x00" in path:
        raise ValueError("Path cannot contain null bytes")
    return path


class SSHClient:
    def __init__(self, host: str):
        self.host = host

    def run(
        self,
        command: str,
        capture_output: bool = True,
        check: bool = True,
        timeout: int | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess:
        ssh_args = ["ssh", self.host, command]
        return subprocess.run(
            ssh_args,
            capture_output=capture_output,
            text=True,
            check=check,
            timeout=timeout,
            input=input,
        )

    def run_script(
        self,
        script: str,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["ssh", self.host, "bash -s"],
            input=script,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )

    def mkdir(self, remote_path: str) -> subprocess.CompletedProcess:
        _validate_path(remote_path)
        return self.run(f"mkdir -p {shlex.quote(remote_path)}")

    def file_exists(self, remote_path: str) -> bool:
        _validate_path(remote_path)
        result = self.run(f"test -f {shlex.quote(remote_path)}", check=False)
        return result.returncode == 0

    def getmtime(self, remote_path: str) -> float | None:
        _validate_path(remote_path)
        quoted = shlex.quote(remote_path)
        result = self.run(
            f"stat -c %Y {quoted} 2>/dev/null || stat -f %m {quoted}", check=False
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
        return None

    def remove_file(self, remote_path: str) -> subprocess.CompletedProcess:
        _validate_path(remote_path)
        return self.run(f"rm -f {shlex.quote(remote_path)}")
