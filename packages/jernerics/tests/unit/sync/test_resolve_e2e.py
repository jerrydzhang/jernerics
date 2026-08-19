import contextlib
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from jernerics.sync.mutagen_sync import (
    MutagenError,
    MutagenNotFound,
    MutagenSync,
    find_mutagen,
)
from jernerics.sync.resolve import SourceSide, resolve_conflicts, sha256_file

try:
    _MUTAGEN = find_mutagen()
    MUTAGEN_AVAILABLE = True
except MutagenNotFound:
    _MUTAGEN = None
    MUTAGEN_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not MUTAGEN_AVAILABLE, reason="mutagen binary not available"
)

# Test sessions deliberately avoid the jernerics-interactive prefix so they can
# never be mistaken for (or terminated as) a live project sync session.
TEST_PREFIX = "jernerics-resolve-e2e"


def _run(*args: str) -> subprocess.CompletedProcess:
    assert _MUTAGEN is not None
    return subprocess.run(
        [_MUTAGEN, *args], capture_output=True, text=True, check=False
    )


class TempSession:
    """A throwaway mutagen session between two local directories."""

    def __init__(self, tmp_path: Path):
        self.alpha = tmp_path / "alpha"
        self.beta = tmp_path / "beta"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.backups = tmp_path / "state"
        self.name = f"{TEST_PREFIX}-{uuid.uuid4().hex[:10]}"
        self.sync = MutagenSync(mutagen_path=_MUTAGEN)
        created = _run(
            "sync",
            "create",
            str(self.alpha),
            str(self.beta),
            "--name",
            self.name,
            "--mode",
            "two-way-safe",
            "--watch-mode",
            "portable",
            "--ignore-vcs",
        )
        assert created.returncode == 0, created.stderr
        if not self.sync.wait_idle(self.name, timeout=30):
            pytest.fail(f"session {self.name} did not go idle after create")

    def stage_conflicts(self, files: dict[str, tuple[str, str]]) -> None:
        for rel, (alpha_text, beta_text) in files.items():
            for root, text in ((self.alpha, alpha_text), (self.beta, beta_text)):
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        assert _run("sync", "resume", self.name).returncode == 0
        self.sync.flush(self.name)
        deadline = time.monotonic() + 20
        wanted = set(files)
        while time.monotonic() < deadline:
            if wanted <= set(self.sync.conflicted_paths(self.name)):
                return
            time.sleep(0.5)
        pytest.fail(
            f"conflicts {sorted(wanted)} never appeared;"
            f" got {self.sync.conflicted_paths(self.name)}"
        )

    def resolve(self, paths, source, **kwargs):
        kwargs.setdefault("assume_yes", True)
        return resolve_conflicts(
            self.sync,
            self.name,
            paths,
            source,
            "e2eproj",
            base_dir=self.backups,
            **kwargs,
        )

    def teardown(self) -> None:
        with contextlib.suppress(MutagenError):
            self.sync.terminate(self.name)


@pytest.fixture
def session(tmp_path):
    temp = TempSession(tmp_path)
    try:
        yield temp
    finally:
        temp.teardown()


class TestEndToEnd:
    def test_local_winner_replaces_cluster_side(self, session):
        session.stage_conflicts({"a.txt": ("local wins", "cluster loses")})

        report = session.resolve(["a.txt"], SourceSide.LOCAL)

        assert report.ok, report.error
        assert (session.alpha / "a.txt").read_text() == "local wins"
        assert (session.beta / "a.txt").read_text() == "local wins"
        backup = report.run_dir / "files" / "a.txt"
        assert backup.read_text() == "cluster loses"
        assert report.outcomes["a.txt"].backup_sha == sha256_file(backup)
        assert report.outcomes["a.txt"].conflict_cleared is True
        assert session.sync.conflicted_paths(session.name) == []

    def test_cluster_winner_replaces_local_side(self, session):
        session.stage_conflicts({"src/mod.py": ("local loses", "cluster wins")})

        report = session.resolve(["src/mod.py"], SourceSide.CLUSTER)

        assert report.ok, report.error
        assert (session.alpha / "src/mod.py").read_text() == "cluster wins"
        assert (session.beta / "src/mod.py").read_text() == "cluster wins"
        assert (report.run_dir / "files" / "src/mod.py").read_text() == "local loses"

    def test_multiple_paths_and_unselected_conflict_remains(self, session):
        session.stage_conflicts(
            {
                "a.txt": ("a local", "a cluster"),
                "src/b.py": ("b local", "b cluster"),
                "c.txt": ("c local", "c cluster"),
            }
        )

        report = session.resolve(["a.txt", "src/b.py"], SourceSide.LOCAL)

        assert report.ok, report.error
        assert report.completed == ["a.txt", "src/b.py"]
        assert (session.beta / "a.txt").read_text() == "a local"
        assert (session.beta / "src/b.py").read_text() == "b local"
        assert (session.beta / "c.txt").read_text() == "c cluster"
        remaining = session.sync.conflicted_paths(session.name)
        assert remaining == ["c.txt"]

    def test_dry_run_changes_nothing(self, session):
        session.stage_conflicts({"a.txt": ("local", "cluster")})

        report = session.resolve(["a.txt"], SourceSide.LOCAL, dry_run=True)

        assert report.ok
        assert (session.alpha / "a.txt").read_text() == "local"
        assert (session.beta / "a.txt").read_text() == "cluster"
        assert not session.backups.exists()
        assert "a.txt" in session.sync.conflicted_paths(session.name)
