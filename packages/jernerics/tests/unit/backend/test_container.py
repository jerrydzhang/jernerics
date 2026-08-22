import os
import shlex
import stat
import subprocess
from pathlib import Path

from jernerics.backend.container import Apptainer, Docker, NoContainer
from jernerics.backend.host import LocalHost
from jernerics.backend.submission import write_env_file

_SENTINEL = "sentinel key $with 'quotes' & spaces"

# Shims standing in for the real container engines: parse --env-file, load
# its KEY=VALUE lines into the environment, drop engine-specific flags and
# the image name, and exec the remaining command. The `set -- "$@" "$keep"`
# idiom rebuilds the positional list with only the inner command's words.
_APPTAINER_SHIM = """\
#!/bin/sh
set -eu
env_file=
image_seen=0
processed=0
total=$#
while [ "$processed" -lt "$total" ]; do
    case "$1" in
        exec|--contain|--nv|--fakeroot)
            shift
            processed=$((processed + 1))
            ;;
        --env-file)
            env_file=$2
            shift 2
            processed=$((processed + 2))
            ;;
        --pwd|--bind)
            shift 2
            processed=$((processed + 2))
            ;;
        *)
            if [ "$image_seen" -eq 0 ]; then
                image_seen=1
                shift
                processed=$((processed + 1))
            else
                keep=$1
                shift
                set -- "$@" "$keep"
                processed=$((processed + 1))
            fi
            ;;
    esac
done
if [ -n "$env_file" ]; then
    while IFS= read -r line; do
        export "${line%%=*}=${line#*=}"
    done < "$env_file"
fi
exec "$@"
"""

_DOCKER_SHIM = """\
#!/bin/sh
set -eu
env_file=
image_seen=0
processed=0
total=$#
while [ "$processed" -lt "$total" ]; do
    case "$1" in
        run|--rm|--network=host)
            shift
            processed=$((processed + 1))
            ;;
        --env-file)
            env_file=$2
            shift 2
            processed=$((processed + 2))
            ;;
        -u|--gpus|-v|-w)
            shift 2
            processed=$((processed + 2))
            ;;
        *)
            if [ "$image_seen" -eq 0 ]; then
                image_seen=1
                shift
                processed=$((processed + 1))
            else
                keep=$1
                shift
                set -- "$@" "$keep"
                processed=$((processed + 1))
            fi
            ;;
    esac
done
if [ -n "$env_file" ]; then
    while IFS= read -r line; do
        export "${line%%=*}=${line#*=}"
    done < "$env_file"
fi
exec "$@"
"""


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


class TestApptainerEnvFile:
    def test_wrap_adds_env_file_flag(self):
        container = Apptainer()
        result = container.wrap(
            "python run.py",
            ["src:/work", "cache:/cache"],
            env_file="/cache/tracking/env",
        )
        assert "--env-file /cache/tracking/env" in result


class TestDockerEnvFile:
    def test_wrap_adds_env_file_flag(self):
        container = Docker()
        result = container.wrap(
            "python run.py",
            ["src:/work", "cache:/cache"],
            env_file="/cache/tracking/env",
        )
        assert "--env-file /cache/tracking/env" in result


class TestNoContainerEnvFile:
    def test_wrap_ignores_env_file(self):
        container = NoContainer()
        result = container.wrap(
            "python run.py",
            ["src:/work"],
            env_file="/cache/tracking/env",
        )
        assert result == "python run.py"


class TestApptainerFakeroot:
    def test_exec_omits_fakeroot_by_default(self):
        # --fakeroot forces a root-mapped user namespace that intermittently
        # fails ("unknown userid") on clusters without /etc/subuid; exec
        # must not use it. It belongs only on the build command.
        container = Apptainer()
        result = container.wrap("python run.py", ["src:/work"])
        assert "--fakeroot" not in result

    def test_fakeroot_remains_opt_in(self):
        container = Apptainer()
        result = container.wrap("python run.py", ["src:/work"], fakeroot=True)
        assert "--fakeroot" in result


class TestEnvFileDelivery:
    """The key reaches the executed process byte-for-byte via --env-file,
    without ever appearing in the engine argv or the wrapped string."""

    def _probe_command(self, tmp_path):
        probe = tmp_path / "probe.py"
        probe.write_text(
            "import os\n"
            "import sys\n"
            'sys.stdout.write(os.environ.get("JERNERICS_API_KEY", "MISSING"))\n'
        )
        return f"python3 {shlex.quote(str(probe))}"

    def _install_shim(self, bin_dir, name, script):
        bin_dir.mkdir(parents=True, exist_ok=True)
        shim = bin_dir / name
        shim.write_text(script)
        shim.chmod(0o755)

    def _run_wrapped(self, bin_dir, wrapped):
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
        return subprocess.run(
            ["sh", "-c", wrapped],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )

    def test_apptainer_shim_sees_env_file_value(self, tmp_path):
        env_file = write_env_file(
            LocalHost(), str(tmp_path), {"JERNERICS_API_KEY": _SENTINEL}
        )
        wrapped = Apptainer().wrap(
            self._probe_command(tmp_path),
            ["src:/work", f"{tmp_path}:/cache"],
            env_file=env_file,
        )
        assert _SENTINEL not in wrapped
        assert "--env-file " in wrapped

        bin_dir = tmp_path / "bin"
        self._install_shim(bin_dir, "apptainer", _APPTAINER_SHIM)

        result = self._run_wrapped(bin_dir, wrapped)
        assert result.stdout == _SENTINEL
        assert stat.S_IMODE(Path(env_file).stat().st_mode) == 0o600

    def test_docker_shim_sees_env_file_value(self, tmp_path):
        env_file = write_env_file(
            LocalHost(), str(tmp_path), {"JERNERICS_API_KEY": _SENTINEL}
        )
        wrapped = Docker(image_name="shimimg", gpu=True).wrap(
            self._probe_command(tmp_path),
            ["src:/work", f"{tmp_path}:/cache"],
            env_file=env_file,
        )
        assert _SENTINEL not in wrapped
        assert "--env-file " in wrapped

        bin_dir = tmp_path / "bin"
        self._install_shim(bin_dir, "docker", _DOCKER_SHIM)

        result = self._run_wrapped(bin_dir, wrapped)
        assert result.stdout == _SENTINEL
        assert stat.S_IMODE(Path(env_file).stat().st_mode) == 0o600
