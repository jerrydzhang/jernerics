import subprocess


class SSHClient:
    def __init__(self, host: str):
        self.host = host

    def run(
        self,
        command: str,
        capture_output: bool = True,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess:
        ssh_args = ["ssh", self.host, command]
        return subprocess.run(
            ssh_args,
            capture_output=capture_output,
            text=True,
            check=check,
            timeout=timeout,
        )

    def run_script(
        self, script: str, check: bool = True
    ) -> subprocess.CompletedProcess:
        return self.run("bash -s", capture_output=True, check=check)

    def mkdir(self, remote_path: str) -> subprocess.CompletedProcess:
        return self.run(f"mkdir -p {remote_path}")

    def file_exists(self, remote_path: str) -> bool:
        result = self.run(f"test -f {remote_path}", check=False)
        return result.returncode == 0

    def getmtime(self, remote_path: str) -> float | None:
        result = self.run(f"stat -c %Y {remote_path}", check=False)
        if result.returncode == 0:
            return float(result.stdout.strip())
        return None

    def remove_file(self, remote_path: str) -> subprocess.CompletedProcess:
        return self.run(f"rm -f {remote_path}")
