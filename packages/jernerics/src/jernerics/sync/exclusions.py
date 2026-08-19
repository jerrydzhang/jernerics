from collections.abc import Sequence
from pathlib import Path

import pathspec

#: Project-root file with Git-style patterns for project-specific excludes.
IGNORE_FILENAME = ".jernericsignore"

#: Pattern sources consulted before the built-in list, in precedence order.
IGNORE_FILE_SOURCES = (".gitignore", IGNORE_FILENAME)

#: Built-in excludes shared by every project-source transfer path.
BUILTIN_EXCLUDES: list[str] = [
    ".git/",
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

#: VCS-dir patterns delegated to mutagen's ``--ignore-vcs`` flag.
VCS_DIR_PATTERNS = frozenset({".git/"})


def project_excludes(project_root: Path) -> list[str]:
    """Return the effective exclude patterns for ``project_root``.

    Sources compose in order — ``.gitignore``, then ``.jernericsignore``, then
    the built-in list — under gitignore last-match-wins semantics, so a later
    ``!pattern`` re-includes what an earlier source excluded. The built-in
    list comes last and can never be negated. Missing ignore files are
    skipped; ``.jernericsignore`` itself is not excluded.
    """
    patterns: list[str] = []
    for name in IGNORE_FILE_SOURCES:
        ignore_file = project_root / name
        if ignore_file.is_file():
            patterns.extend(ignore_file.read_text().splitlines())
    patterns.extend(BUILTIN_EXCLUDES)
    return patterns


def compile_excludes(patterns: Sequence[str]) -> pathspec.PathSpec:
    """Compile gitignore-style patterns into a matchable spec."""
    return pathspec.PathSpec.from_lines("gitignore", patterns)


def should_include(rel_path: str, spec: pathspec.PathSpec) -> bool:
    """Apply the effective policy to one project-relative POSIX path."""
    return not spec.match_file(rel_path)


def mutagen_ignores(patterns: Sequence[str], *, ignore_vcs: bool = True) -> list[str]:
    """Filter patterns for mutagen's repeated ``-i`` flags.

    With ``ignore_vcs`` (the default), VCS-dir patterns are dropped: those
    directories are handled by mutagen's ``--ignore-vcs`` flag, not by
    pattern entries.
    """
    if not ignore_vcs:
        return list(patterns)
    return [p for p in patterns if p not in VCS_DIR_PATTERNS]
