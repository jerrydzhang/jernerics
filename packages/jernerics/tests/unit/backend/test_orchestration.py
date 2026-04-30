from jernerics.backend.orchestration import _compose_build_script

_BUILD_CMD = [
    "apptainer",
    "build",
    "--fakeroot",
    "--force",
    "container.sif",
    "container.def",
]
_BUILD_STR = "apptainer build --fakeroot --force container.sif container.def"


class TestComposeBuildScript:
    def test_build_with_build_dir_uses_apptainer_tmpdir(self):
        script = _compose_build_script(
            build_command=_BUILD_CMD,
            remote_dir="/scratch/proj",
            marker_path="/cache/.build_marker",
            build_dir="/dev/shm/build/my-project",
        )
        assert "export APPTAINER_TMPDIR=/dev/shm/build/my-project" in script
        assert "cd /scratch/proj" in script
        assert _BUILD_STR in script
        assert "rm -rf /dev/shm/build/my-project" in script
        assert "touch /cache/.build_marker" in script
        # Creates the tmpdir before export
        assert "mkdir -p /dev/shm/build/my-project" in script
        # No staging copies
        assert "cp " not in script

    def test_in_place_build(self):
        script = _compose_build_script(
            build_command=_BUILD_CMD,
            remote_dir="/scratch/proj",
            marker_path="/cache/.build_marker",
            build_dir=None,
        )
        lines = script.splitlines()
        assert lines[0] == "set -e"
        assert "cd /scratch/proj" in script
        assert _BUILD_STR in script
        assert "touch /cache/.build_marker" in script
        assert "cp " not in script
        assert "rm -rf" not in script

    def test_build_with_build_dir_cleans_up(self):
        script = _compose_build_script(
            build_command=_BUILD_CMD,
            remote_dir="/scratch/proj",
            marker_path="/cache/.build_marker",
            build_dir="/dev/shm/build/my-project",
        )
        # Cleanup happens after build
        lines = script.splitlines()
        build_idx = next(i for i, l in enumerate(lines) if _BUILD_STR in l)
        cleanup_idx = next(i for i, l in enumerate(lines) if "rm -rf" in l)
        assert cleanup_idx > build_idx

    def test_marker_uses_original_path(self):
        script = _compose_build_script(
            build_command=_BUILD_CMD,
            remote_dir="/scratch/proj",
            marker_path="/cache/proj/.build_marker",
            build_dir="/dev/shm/build/proj",
        )
        assert "touch /cache/proj/.build_marker" in script
