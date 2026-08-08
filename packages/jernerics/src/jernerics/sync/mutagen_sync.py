"""Continuous bidirectional project-source sync via the mutagen CLI.

Mutagen is an **optional** dependency — jernerics must not require it. Callers
check :func:`MutagenSync.available` (or :func:`find_mutagen`) before use and fall
back to :class:`jernerics.backend.project_sync.ProjectSync` (tar+scp one-shot)
when mutagen is absent.

This module only wraps the ``mutagen`` command-line tool; it does not bundle or
install it. Mutagen's agent is auto-injected over SSH, so nothing needs to be
installed on the remote host.

The lifecycle an interactive session drives is::

    sync = MutagenSync()
    sync.start(local_dir, host, remote_dir, name=session_name(project))
    ... session runs; edits flow both ways ...
    sync.terminate(session_name(project))

For NFS-backed remote home directories (where inotify does not fire) the default
``--watch-mode portable`` makes the remote endpoint poll. Local-side watching
stays real-time.

See the beads task ``jernerics-jernerics-interactive-uyy.2`` for the design.
"""

import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

MUTAGEN_BIN = "mutagen"

#: Prefix for every jernerics-managed sync session. Orphan detection matches on
#: this. Mirrors the SLURM job-name pattern ``jernerics-interactive-<project>``
#: used by the interactive allocation (task ``...-uyy.1``) so a sync session can
#: be paired with its job.
SESSION_PREFIX = "jernerics-interactive"

#: Session status reported by mutagen once an endpoint is idle and fully synced.
#: Any other status (``Saving``, ``Connecting``, ``Conflict resolution
#: required`` ...) means initial convergence is not yet reached or needs action.
CONVERGED_STATUS = "Watching"

#: Polling interval (seconds) while waiting for convergence.
DEFAULT_POLL_INTERVAL = 1.0

#: How long :meth:`MutagenSync.start` waits for the initial sync to settle.
DEFAULT_CONVERGENCE_TIMEOUT = 60

#: Ignore patterns for interactive sync. ``--ignore-vcs`` handles ``.git`` etc.
#: separately, so VCS dirs are intentionally absent here. Mirrors
#: ``ProjectSync.DEFAULT_EXCLUDES`` plus interactive-only paths (``pools/``,
#: ``logs/``). Applied by mutagen continuously in both directions.
INTERACTIVE_EXCLUDES: list[str] = [
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
]

#: mutagen ``sync list`` template emitting one tab-delimited record per session:
#: name, status, alpha path, beta path, alpha connected, beta connected.
_LIST_TEMPLATE = (
    "{{range .}}{{.Name}}\t{{.Status}}\t{{.Alpha.Path}}"
    "\t{{.Beta.Path}}\t{{.Alpha.Connected}}\t{{.Beta.Connected}}\n{{end}}"
)


class MutagenError(Exception):
    """A mutagen command failed."""


class MutagenNotFound(MutagenError):
    """The mutagen binary is not installed or not on PATH."""


def find_mutagen() -> str:
    """Return the absolute mutagen binary path, or raise ``MutagenNotFound``."""
    import shutil

    path = shutil.which(MUTAGEN_BIN)
    if path is None:
        raise MutagenNotFound(
            "mutagen not found on PATH. Install it (e.g. "
            "`brew install mutagen-io/mutagen/mutagen` or download from "
            "https://mutagen.io) to enable continuous interactive sync. "
            "Falling back to one-shot tar+scp."
        )
    return path


def _sanitize(name: str) -> str:
    """Make ``name`` safe for a mutagen session label (alnum/``-_.`` only)."""
    cleaned = "".join(c if c.isalnum() or c in "-_." else "-" for c in name)
    cleaned = cleaned.strip("-_.")
    return cleaned or "project"


def session_name(project_name: str) -> str:
    """Canonical sync-session name for a project.

    ``jernerics-interactive-<project>`` — matches the interactive allocation's
    SLURM job name so an orphan sync session can be paired with (or distinguished
    from) a live job.
    """
    return f"{SESSION_PREFIX}-{_sanitize(project_name)}"


@dataclass
class SessionInfo:
    """One mutagen sync session, as reported by ``mutagen sync list``."""

    name: str
    status: str
    alpha_path: str
    beta_path: str
    alpha_connected: bool
    beta_connected: bool

    @property
    def is_jernerics(self) -> bool:
        return self.name.startswith(SESSION_PREFIX)


def parse_list_output(output: str) -> list[SessionInfo]:
    """Parse ``mutagen sync list --template`` output into session records.

    Empty output (no sessions) yields an empty list. Malformed lines are skipped
    rather than raising — a partial parse is more useful to callers doing orphan
    detection than an explosion.
    """
    sessions: list[SessionInfo] = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        name, status, alpha_path, beta_path, alpha_conn, beta_conn = fields
        sessions.append(
            SessionInfo(
                name=name,
                status=status,
                alpha_path=alpha_path,
                beta_path=beta_path,
                alpha_connected=alpha_conn == "true",
                beta_connected=beta_conn == "true",
            )
        )
    return sessions


def is_converged(session: SessionInfo) -> bool:
    """True once a session has finished its initial sync and is idle.

    Requires both endpoints connected and status at idle. Conflict states
    (``Conflict resolution required``, ``Waiting for confirmation``) are
    intentionally *not* converged — two-way-safe surfaces them rather than
    silently overwriting.
    """
    return (
        session.status == CONVERGED_STATUS
        and session.alpha_connected
        and session.beta_connected
    )


class MutagenSync:
    """Wrap the mutagen CLI for continuous bidirectional sync.

    Construct freely even when mutagen is absent; methods that invoke mutagen
    raise :class:`MutagenNotFound` so callers can fall back. Check
    :meth:`available` first to choose a strategy without catching exceptions.
    """

    def __init__(
        self,
        mutagen_path: str | None = None,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._mutagen_path = mutagen_path
        self.poll_interval = poll_interval

    @staticmethod
    def available() -> bool:
        """True if the mutagen binary is on PATH."""
        import shutil

        return shutil.which(MUTAGEN_BIN) is not None

    def _bin(self) -> str:
        return self._mutagen_path or find_mutagen()

    def _run(self, args: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run([self._bin(), *args], **kwargs)

    def build_create_command(
        self,
        local_dir: str | Path,
        remote_host: str,
        remote_dir: str,
        *,
        name: str,
        excludes: Sequence[str] = INTERACTIVE_EXCLUDES,
        sync_mode: str = "two-way-safe",
        watch_mode: str = "portable",
        ignore_vcs: bool = True,
    ) -> list[str]:
        """Build the ``mutagen sync create`` argv (without the leading binary).

        Pure function — safe to unit-test without invoking mutagen.
        """
        beta = f"{remote_host}:{remote_dir}"
        cmd: list[str] = [
            "sync",
            "create",
            str(local_dir),
            beta,
            "--name",
            name,
            "--mode",
            sync_mode,
            "--watch-mode",
            watch_mode,
        ]
        if ignore_vcs:
            cmd.append("--ignore-vcs")
        for pattern in excludes:
            cmd += ["-i", pattern]
        return cmd

    def start(
        self,
        local_dir: str | Path,
        remote_host: str,
        remote_dir: str,
        *,
        name: str,
        excludes: Sequence[str] = INTERACTIVE_EXCLUDES,
        sync_mode: str = "two-way-safe",
        watch_mode: str = "portable",
        ignore_vcs: bool = True,
        convergence_timeout: int = DEFAULT_CONVERGENCE_TIMEOUT,
    ) -> str:
        """Create a session and block until initial convergence.

        Returns the session ``name``. Raises :class:`MutagenError` if creation
        fails or the session does not reach idle within ``convergence_timeout``.
        """
        cmd = self.build_create_command(
            local_dir,
            remote_host,
            remote_dir,
            name=name,
            excludes=excludes,
            sync_mode=sync_mode,
            watch_mode=watch_mode,
            ignore_vcs=ignore_vcs,
        )
        result = self._run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise MutagenError(
                f"mutagen sync create failed (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        if not self.wait_converged(name, timeout=convergence_timeout):
            raise MutagenError(
                f"sync session {name!r} did not converge within "
                f"{convergence_timeout}s (last status see `mutagen sync list`)"
            )
        return name

    def wait_converged(self, name: str, timeout: int = 60) -> bool:
        """Poll until session ``name`` is converged, or ``timeout`` elapses.

        Returns True on convergence, False on timeout. Raises
        :class:`MutagenError` if the session cannot be found at all.
        """
        deadline = time.monotonic() + timeout
        seen = False
        while time.monotonic() < deadline:
            sessions = {s.name: s for s in self.list_sessions()}
            session = sessions.get(name)
            if session is None:
                # The daemon may take a moment to register a freshly created
                # session; keep polling until we've seen it at least once.
                time.sleep(self.poll_interval)
                continue
            seen = True
            if is_converged(session):
                return True
            time.sleep(self.poll_interval)
        if not seen:
            raise MutagenError(f"sync session {name!r} not found by mutagen")
        return False

    def terminate(self, name: str) -> None:
        """Terminate session ``name``. Idempotent: no error if already gone.

        ``mutagen sync terminate`` exits non-zero when the named session does
        not exist, so we treat the "did not match any sessions" error as
        success — teardown must not blow up on a session that already ended.
        """
        result = self._run(
            ["sync", "terminate", name],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            msg = (result.stderr or result.stdout).strip()
            if "did not match any sessions" in msg:
                return
            raise MutagenError(
                f"mutagen sync terminate {name!r} failed (exit "
                f"{result.returncode}): {msg}"
            )

    def list_sessions(self, name: str | None = None) -> list[SessionInfo]:
        """List sync sessions, optionally filtered to ``name``.

        Filtering is done client-side: ``mutagen sync list <name>`` *errors*
        (exit 1) when the name is absent rather than returning empty, which
        would break convergence polling. Fetching all sessions and filtering
        here keeps ``wait_converged`` robust while the daemon registers a
        freshly created session.
        """
        result = self._run(
            ["sync", "list", "--template", _LIST_TEMPLATE],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise MutagenError(
                f"mutagen sync list failed (exit {result.returncode}): "
                f"{(result.stderr or result.stdout).strip()}"
            )
        sessions = parse_list_output(result.stdout)
        if name is not None:
            sessions = [s for s in sessions if s.name == name]
        return sessions

    def find_orphans(
        self, alive_names: Iterable[str] | None = None
    ) -> list[SessionInfo]:
        """Return jernerics sync sessions with no matching live session/job.

        ``alive_names`` is the set of session names that still correspond to a
        live interactive allocation (typically just the current project's
        :func:`session_name`). Any jernerics-prefixed session not in that set is
        considered orphaned. Used at interactive startup to surface stale sync
        sessions left behind by crashed or scancelled runs.
        """
        alive = set(alive_names or ())
        return [
            s for s in self.list_sessions() if s.is_jernerics and s.name not in alive
        ]

    def terminate_orphans(self, alive_names: Iterable[str] | None = None) -> list[str]:
        """Terminate every orphaned jernerics session. Returns the names removed."""
        removed: list[str] = []
        for session in self.find_orphans(alive_names):
            self.terminate(session.name)
            removed.append(session.name)
        return removed
