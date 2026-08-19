from pathlib import Path

from jernerics.backend.project_sync import _collect_files
from jernerics.sync.exclusions import (
    BUILTIN_EXCLUDES,
    IGNORE_FILENAME,
    compile_excludes,
    mutagen_ignores,
    project_excludes,
    should_include,
)
from jernerics.sync.mutagen_sync import MutagenSync

#: Former ``ProjectSync.DEFAULT_EXCLUDES`` (project_sync.py).
FORMER_PROJECT_SYNC_EXCLUDES = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    "*.sif",
    ".cache/",
    "results/",
    ".venv/",
    "venv/",
    "*.egg-info/",
    ".eggs/",
    "build/",
    "dist/",
    ".mypy_cache/",
    ".ruff_cache/",
]

#: Former ``INTERACTIVE_EXCLUDES`` (mutagen_sync.py).
FORMER_INTERACTIVE_EXCLUDES = [
    "__pycache__/",
    "*.pyc",
    "*.sif",
    ".cache/",
    "results/",
    "pools/",
    "logs/",
    ".venv/",
    "venv/",
    "*.egg-info/",
    ".eggs/",
    "build/",
    "dist/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".hypothesis/",
    ".pytest_cache/",
    ".direnv/",
]


def _spec(root: Path):
    return compile_excludes(project_excludes(root))


class TestBuiltinExcludes:
    def test_preserves_former_project_sync_list(self):
        assert set(FORMER_PROJECT_SYNC_EXCLUDES) <= set(BUILTIN_EXCLUDES)

    def test_preserves_former_interactive_list(self):
        assert set(FORMER_INTERACTIVE_EXCLUDES) <= set(BUILTIN_EXCLUDES)

    def test_no_tool_specific_exceptions(self):
        assert ".beads" not in BUILTIN_EXCLUDES
        assert ".beads/" not in BUILTIN_EXCLUDES


class TestProjectExcludes:
    def test_no_ignore_files_yields_builtin_only(self, tmp_path):
        assert project_excludes(tmp_path) == BUILTIN_EXCLUDES

    def test_composition_order_gitignore_then_jernericsignore_then_builtin(
        self, tmp_path
    ):
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / IGNORE_FILENAME).write_text("!keep.log\nscratch/\n")

        assert project_excludes(tmp_path) == [
            "*.log",
            "!keep.log",
            "scratch/",
            *BUILTIN_EXCLUDES,
        ]

    def test_missing_gitignore_is_graceful(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("scratch/\n")

        assert project_excludes(tmp_path) == ["scratch/", *BUILTIN_EXCLUDES]

    def test_missing_jernericsignore_is_graceful(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n")

        assert project_excludes(tmp_path) == ["*.log", *BUILTIN_EXCLUDES]

    def test_nonexistent_root_is_graceful(self, tmp_path):
        assert project_excludes(tmp_path / "missing") == BUILTIN_EXCLUDES


class TestPolicyMatching:
    def test_plain_file_and_dir_patterns(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("secret.env\ndata/\n")
        spec = _spec(tmp_path)

        assert not should_include("secret.env", spec)
        assert not should_include("src/secret.env", spec)
        assert not should_include("data/model.bin", spec)
        assert should_include("src/main.py", spec)

    def test_rooted_pattern_matches_root_only(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("/scratch.txt\n")
        spec = _spec(tmp_path)

        assert not should_include("scratch.txt", spec)
        assert should_include("nested/scratch.txt", spec)

    def test_negation_reincludes_gitignore_excluded(self, tmp_path):
        (tmp_path / ".gitignore").write_text("*.log\n")
        (tmp_path / IGNORE_FILENAME).write_text("!keep.log\n")
        spec = _spec(tmp_path)

        assert should_include("keep.log", spec)
        assert not should_include("debug.log", spec)

    def test_negation_cannot_reinclude_builtin_excluded(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("!results/\n!.git/\n")
        spec = _spec(tmp_path)

        assert not should_include("results/out.json", spec)
        assert not should_include(".git/config", spec)

    def test_gitignore_negation_cannot_reinclude_builtin_excluded(self, tmp_path):
        (tmp_path / ".gitignore").write_text("!results/\n")
        spec = _spec(tmp_path)

        assert not should_include("results/out.json", spec)

    def test_jernericsignore_itself_syncs(self, tmp_path):
        (tmp_path / IGNORE_FILENAME).write_text("scratch/\n")
        spec = _spec(tmp_path)

        assert should_include(IGNORE_FILENAME, spec)


class TestMutagenIgnores:
    def test_drops_vcs_patterns_by_default(self):
        assert mutagen_ignores([".git/", "results/"]) == ["results/"]

    def test_keeps_vcs_patterns_when_vcs_not_ignored(self):
        assert mutagen_ignores([".git/", "results/"], ignore_vcs=False) == [
            ".git/",
            "results/",
        ]


class TestTarMutagenEquivalence:
    def _build_tree(self, root: Path) -> set[str]:
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("[core]")
        (root / ".gitignore").write_text("*.log\n")
        (root / IGNORE_FILENAME).write_text("!keep.log\nscratch/\n!results/\n")
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("main")
        (root / "debug.log").write_text("log")
        (root / "keep.log").write_text("kept")
        (root / "scratch").mkdir()
        (root / "scratch" / "x.bin").write_text("x")
        (root / "results").mkdir()
        (root / "results" / "out.json").write_text("{}")
        return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}

    def _i_args(self, cmd: list[str]) -> list[str]:
        return [arg for i, arg in enumerate(cmd) if cmd[i - 1] == "-i"]

    def test_collect_files_and_mutagen_i_args_agree(self, tmp_path):
        all_files = self._build_tree(tmp_path)
        tar_files = {
            p.relative_to(tmp_path).as_posix()
            for p in _collect_files(tmp_path, _spec(tmp_path))
        }

        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(tmp_path, "h", "/r", name="n", ignore_vcs=False)
        assert self._i_args(cmd) == project_excludes(tmp_path)
        mutagen_spec = compile_excludes(self._i_args(cmd))

        for rel in all_files:
            assert (rel in tar_files) == should_include(rel, mutagen_spec), rel

        assert tar_files == {".gitignore", IGNORE_FILENAME, "src/main.py", "keep.log"}

    def test_default_ignore_vcs_delegates_git_to_flag(self, tmp_path):
        self._build_tree(tmp_path)

        sync = MutagenSync(mutagen_path="/p/mutagen")
        cmd = sync.build_create_command(tmp_path, "h", "/r", name="n")

        assert "--ignore-vcs" in cmd
        assert ".git/" not in self._i_args(cmd)
        assert self._i_args(cmd) == mutagen_ignores(project_excludes(tmp_path))
