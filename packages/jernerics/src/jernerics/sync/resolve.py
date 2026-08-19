import enum
import hashlib
import json
import os
import secrets
import shlex
import shutil
import stat as stat_mod
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from jernerics.sync.mutagen_sync import MutagenError, MutagenSync, SessionInfo


class ResolveError(Exception):
    """A resolve invocation must not proceed; nothing has been changed."""


class EndpointError(Exception):
    """An endpoint filesystem operation failed or returned junk."""


class SourceSide(enum.Enum):
    """Which endpoint's content wins; one source per invocation."""

    LOCAL = "local"
    CLUSTER = "cluster"


#: Where losers are backed up: ``<state>/jernerics/sync-backups``.
BACKUPS_DIRNAME = "sync-backups"


def backups_root() -> Path:
    """Local backup root honoring ``$XDG_STATE_HOME`` (default ~/.local/state)."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "jernerics" / BACKUPS_DIRNAME


def new_run_id() -> str:
    """UTC-unique run identifier: timestamp plus random suffix."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def normalize_rel(path: str) -> str:
    """Normalize one user-supplied path to a clean project-relative POSIX path.

    Refuses empty, ``.``-only, absolute, and ``..``-traversing inputs — these
    could escape a project root. Raises :class:`ResolveError`.
    """
    if not path:
        raise ResolveError("empty path")
    if PurePosixPath(path).is_absolute():
        raise ResolveError(f"absolute paths are not allowed: {path!r}")
    parts = PurePosixPath(path).parts
    if not parts or parts == (".",):
        raise ResolveError(f"path {path!r} does not name a file")
    if ".." in parts:
        raise ResolveError(f"parent traversal (..) is not allowed: {path!r}")
    return str(PurePosixPath(*parts))


def normalize_conflict(path: str) -> str:
    """Normalize a mutagen conflict root the way user paths are normalized."""
    return str(PurePosixPath(path))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Endpoint(Protocol):
    """One side of a sync session: a local directory or an SSH remote root."""

    side: str

    def describe(self) -> str: ...
    def kind_and_size(self, rel: str) -> tuple[str, int]: ...
    def checksum(self, rel: str) -> str: ...
    def free_bytes(self) -> int: ...
    def fetch_to(self, rel: str, dest: Path) -> None: ...
    def install(self, rel: str, src: Path, expected_sha: str) -> None: ...


@dataclass
class LocalEndpoint:
    root: Path
    side: str

    def describe(self) -> str:
        return str(self.root)

    def _abs(self, rel: str) -> Path:
        return self.root.joinpath(*PurePosixPath(rel).parts)

    def kind_and_size(self, rel: str) -> tuple[str, int]:
        try:
            st = os.lstat(self._abs(rel))
        except FileNotFoundError:
            return ("missing", 0)
        if stat_mod.S_ISLNK(st.st_mode):
            return ("symlink", 0)
        if stat_mod.S_ISDIR(st.st_mode):
            return ("directory", 0)
        if stat_mod.S_ISREG(st.st_mode):
            return ("file", st.st_size)
        return ("other", 0)

    def checksum(self, rel: str) -> str:
        return sha256_file(self._abs(rel))

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def fetch_to(self, rel: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._abs(rel), dest)

    def install(self, rel: str, src: Path, expected_sha: str) -> None:
        dest = self._abs(rel)
        tmp = dest.parent / f"{dest.name}.jernerics-resolve-{secrets.token_hex(4)}.tmp"
        try:
            shutil.copyfile(src, tmp)
            if sha256_file(tmp) != expected_sha:
                raise EndpointError(
                    f"temp copy {tmp} failed checksum verification for {rel!r}"
                )
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)


@dataclass
class SSHEndpoint:
    target: str
    root: str
    side: str

    def __post_init__(self) -> None:
        if not self.target or not self.root.startswith("/"):
            raise EndpointError(f"invalid SSH endpoint {self.target}:{self.root}")

    def describe(self) -> str:
        return f"{self.target}:{self.root}"

    def _remote(self, rel: str) -> str:
        return f"{self.root}/{PurePosixPath(rel).as_posix()}"

    def _ssh(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        quoted = " ".join(shlex.quote(a) for a in args)
        return subprocess.run(
            ["ssh", "-o", "LogLevel=ERROR", self.target, quoted],
            capture_output=True,
            text=True,
            check=False,
        )

    def kind_and_size(self, rel: str) -> tuple[str, int]:
        result = self._ssh(["stat", "-c", "%F|%s", "--", self._remote(rel)])
        if result.returncode == 255:
            raise EndpointError(
                f"ssh to {self.target} failed: {(result.stderr or '').strip()}"
            )
        if result.returncode != 0:
            return ("missing", 0)
        kind_raw, _, size = result.stdout.strip().partition("|")
        kind = {
            "regular file": "file",
            "directory": "directory",
            "symbolic link": "symlink",
        }.get(kind_raw)
        if kind is None:
            return ("other", 0)
        return (kind, int(size) if size.isdigit() else 0)

    def checksum(self, rel: str) -> str:
        result = self._ssh(["sha256sum", "--", self._remote(rel)])
        if result.returncode != 0:
            raise EndpointError(
                f"sha256sum failed on {self.describe()} for {rel!r}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        fields = result.stdout.split()
        if not fields or len(fields[0]) != 64:
            raise EndpointError(f"unparseable sha256sum output for {rel!r}")
        return fields[0]

    def free_bytes(self) -> int:
        result = self._ssh(["df", "-B1", "--output=avail", "--", self.root])
        if result.returncode != 0:
            raise EndpointError(
                f"df failed on {self.describe()}: {(result.stderr or '').strip()}"
            )
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        try:
            return int(lines[-1].split()[0])
        except (IndexError, ValueError) as e:
            raise EndpointError(f"unparseable df output on {self.describe()}") from e

    def fetch_to(self, rel: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        remote = f"{self.target}:{shlex.quote(self._remote(rel))}"
        result = subprocess.run(
            ["scp", "-q", remote, os.fspath(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise EndpointError(
                f"scp download of {remote} failed: "
                f"{(result.stderr or result.stdout).strip()}"
            )

    def install(self, rel: str, src: Path, expected_sha: str) -> None:
        remote_tmp = f"{self._remote(rel)}.jernerics-resolve-{secrets.token_hex(4)}.tmp"
        remote = f"{self.target}:{shlex.quote(remote_tmp)}"
        result = subprocess.run(
            ["scp", "-q", os.fspath(src), remote],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise EndpointError(
                f"scp upload to {remote} failed: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        try:
            digest_result = self._ssh(["sha256sum", "--", remote_tmp])
            fields = digest_result.stdout.split()
            if digest_result.returncode != 0 or not fields:
                raise EndpointError(
                    f"could not checksum temp {remote_tmp}: "
                    f"{(digest_result.stderr or digest_result.stdout).strip()}"
                )
            if fields[0] != expected_sha:
                raise EndpointError(
                    f"temp copy {remote_tmp} failed checksum verification"
                )
            move = self._ssh(["mv", "-f", "--", remote_tmp, self._remote(rel)])
            if move.returncode != 0:
                raise EndpointError(
                    f"atomic rename failed for {rel!r}: "
                    f"{(move.stderr or move.stdout).strip()}"
                )
        except BaseException:
            self._ssh(["rm", "-f", "--", remote_tmp])
            raise


def endpoint_for(path: str, side: str) -> Endpoint:
    """Build the endpoint for one session side (``local``=alpha, ``cluster``=beta).

    ``user@host:/abs/root`` is an SSH endpoint; anything else is local.
    """
    head, sep, tail = path.partition(":")
    if sep and head and "/" not in head:
        return SSHEndpoint(head, tail, side)
    return LocalEndpoint(Path(path), side)


def endpoints_from_session(record: SessionInfo) -> tuple[Endpoint, Endpoint]:
    return (
        endpoint_for(record.alpha_path, "local"),
        endpoint_for(record.beta_path, "cluster"),
    )


@dataclass
class PathPlan:
    """Everything known about one path before any mutation happens."""

    rel: str
    source: SourceSide
    winner_size: int
    winner_sha: str
    loser_size: int
    loser_sha: str
    backup_path: Path | None = None

    @property
    def direction(self) -> str:
        if self.source is SourceSide.LOCAL:
            return "local -> cluster"
        return "cluster -> local"

    @property
    def loser_side(self) -> str:
        return "cluster" if self.source is SourceSide.LOCAL else "local"


@dataclass
class PathOutcome:
    rel: str
    status: str = "untouched"
    mutation: str = "not attempted"
    backup_sha: str | None = None
    conflict_cleared: bool | None = None
    error: str | None = None


@dataclass
class ResolveReport:
    session: str
    source: SourceSide
    local_root: str
    cluster_root: str
    plans: list[PathPlan] = field(default_factory=list)
    outcomes: dict[str, PathOutcome] = field(default_factory=dict)
    run_dir: Path | None = None
    started_utc: str = ""
    finished_utc: str = ""
    dry_run: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        if self.dry_run:
            return self.error is None
        return self.error is None and all(
            o.status == "completed" for o in self.outcomes.values()
        )

    @property
    def completed(self) -> list[str]:
        return [r for r, o in self.outcomes.items() if o.status == "completed"]

    @property
    def unresolved(self) -> list[str]:
        return [r for r, o in self.outcomes.items() if o.status == "unresolved"]

    @property
    def untouched(self) -> list[str]:
        return [r for r, o in self.outcomes.items() if o.status == "untouched"]


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


def _dedupe(paths: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in paths:
        rel = normalize_rel(raw)
        if rel not in seen:
            seen.add(rel)
            ordered.append(rel)
    return ordered


def _require_file(endpoint: Endpoint, rel: str) -> int:
    """Return the endpoint's size for ``rel``, refusing non-regular entries."""
    kind, size = endpoint.kind_and_size(rel)
    if kind != "file":
        labels = {
            "missing": "missing",
            "directory": "a directory",
            "symlink": "a symlink",
            "other": "not a regular file",
        }
        raise ResolveError(
            f"{rel!r} is {labels.get(kind, kind)} on the {endpoint.side} side"
            f" ({endpoint.describe()}); both sides must hold regular files"
        )
    return size


def resolve_conflicts(
    sync: MutagenSync,
    session: str,
    paths: Sequence[str],
    source: SourceSide,
    project: str,
    *,
    base_dir: Path | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    is_interactive: bool | None = None,
    confirm: Callable[[str], bool] | None = None,
    on_plans: Callable[[list[PathPlan], Path], None] | None = None,
    idle_timeout: int = 60,
) -> ResolveReport:
    """Resolve explicit sync conflicts by overwriting one side from the other.

    The safety contract, in order: every path is validated against the
    session's current conflict list and both endpoints (regular files only);
    every path is previewed; one confirmation covers the whole invocation;
    every losing copy is backed up locally and checksum-verified before the
    first overwrite; each mutation re-hashes both sides to detect races and
    lands via temp-sibling plus atomic rename; any failure stops immediately
    and never rolls back; finally the existing session is flushed and every
    selected path must have left the conflict list. The session itself is
    never restarted or replaced.
    """
    started = _now_utc()
    if not paths:
        raise ResolveError("at least one PATH is required")

    rels = _dedupe(paths)

    records = [s for s in sync.list_sessions() if s.name == session]
    if not records:
        raise ResolveError(f"sync session {session!r} not found")
    record = records[0]
    if not (record.alpha_connected and record.beta_connected):
        raise ResolveError(
            f"sync session {session!r} has a disconnected endpoint;"
            " resolve requires both endpoints reachable"
        )

    conflicted = {normalize_conflict(p) for p in sync.conflicted_paths(session)}
    absent = [r for r in rels if r not in conflicted]
    if absent:
        raise ResolveError(
            f"not in the current conflict list of {session!r}: {', '.join(absent)}"
        )

    local_ep, cluster_ep = endpoints_from_session(record)
    winner, loser = (
        (local_ep, cluster_ep) if source is SourceSide.LOCAL else (cluster_ep, local_ep)
    )

    plans: list[PathPlan] = []
    for rel in rels:
        plans.append(
            PathPlan(
                rel=rel,
                source=source,
                winner_size=_require_file(winner, rel),
                winner_sha=winner.checksum(rel),
                loser_size=_require_file(loser, rel),
                loser_sha=loser.checksum(rel),
            )
        )

    run_dir = (base_dir or backups_root()) / project / new_run_id()
    for plan in plans:
        plan.backup_path = run_dir / "files" / plan.rel

    _preflight_space(plans, loser, run_dir)

    report = ResolveReport(
        session=session,
        source=source,
        local_root=local_ep.describe(),
        cluster_root=cluster_ep.describe(),
        plans=plans,
        outcomes={rel: PathOutcome(rel=rel) for rel in rels},
        run_dir=None,
        started_utc=started,
        dry_run=dry_run,
    )

    if on_plans is not None:
        on_plans(plans, run_dir)

    if dry_run:
        report.finished_utc = _now_utc()
        return report

    _confirm_invocation(
        len(plans), source, run_dir, assume_yes, is_interactive, confirm
    )

    report.run_dir = run_dir
    try:
        _backup_losers(plans, loser, report)
        if report.error is None:
            _mutate(plans, winner, loser, report)
        if report.error is None:
            _verify_conflicts_cleared(sync, session, report, idle_timeout)
    finally:
        report.finished_utc = _now_utc()
        if report.run_dir is not None:
            _write_manifest(report, local_ep, cluster_ep)
    return report


def _preflight_space(plans: list[PathPlan], loser: Endpoint, run_dir: Path) -> None:
    try:
        dest_free = loser.free_bytes()
    except EndpointError as e:
        raise ResolveError(
            f"could not determine free space on the destination side: {e}"
        ) from e
    peak_temp = max(p.winner_size for p in plans)
    if dest_free < peak_temp:
        raise ResolveError(
            f"destination has {dest_free} bytes free but the largest transfer"
            f" needs {peak_temp}; refusing"
        )

    local_needed = sum(p.loser_size for p in plans) + sum(
        p.winner_size for p in plans if p.source is SourceSide.CLUSTER
    )
    probe = run_dir.parents[1]
    while not probe.exists():
        probe = probe.parent
    local_free = shutil.disk_usage(probe).free
    if local_free < local_needed:
        raise ResolveError(
            f"local backup storage has {local_free} bytes free but backups"
            f" and staging need {local_needed}; refusing"
        )


def _confirm_invocation(
    count: int,
    source: SourceSide,
    run_dir: Path,
    assume_yes: bool,
    is_interactive: bool | None,
    confirm: Callable[[str], bool] | None,
) -> None:
    if assume_yes:
        return
    interactive = sys.stdin.isatty() if is_interactive is None else is_interactive
    if not interactive:
        raise ResolveError("noninteractive invocation requires --yes")
    question = (
        f"Apply {count} resolution(s) with {source.value} as winner"
        f" (losers backed up under {run_dir})? [y/N] "
    )
    if confirm is None:

        def _ask(msg: str) -> bool:
            return input(msg).strip().lower() in ("y", "yes")

        confirm = _ask
    if not confirm(question):
        raise ResolveError("declined")


def _backup_losers(
    plans: list[PathPlan], loser: Endpoint, report: ResolveReport
) -> None:
    assert report.run_dir is not None
    try:
        for plan in plans:
            assert plan.backup_path is not None
            loser.fetch_to(plan.rel, plan.backup_path)
            backup_sha = sha256_file(plan.backup_path)
            if backup_sha != plan.loser_sha:
                report.outcomes[plan.rel].status = "unresolved"
                report.outcomes[plan.rel].error = "backup checksum mismatch"
                report.error = (
                    f"backup of {plan.rel!r} does not match its preflight"
                    " checksum; nothing was mutated"
                )
                return
            report.outcomes[plan.rel].backup_sha = backup_sha
    except (EndpointError, OSError) as e:
        report.error = f"backup of losers failed ({e}); nothing was mutated"
        failed = next(
            (p.rel for p in plans if report.outcomes[p.rel].backup_sha is None),
            None,
        )
        if failed is not None:
            report.outcomes[failed].status = "unresolved"
            report.outcomes[failed].error = f"backup failed: {e}"


def _mutate(
    plans: list[PathPlan],
    winner: Endpoint,
    loser: Endpoint,
    report: ResolveReport,
) -> None:
    assert report.run_dir is not None
    staging_root = report.run_dir / "staging"
    for plan in plans:
        outcome = report.outcomes[plan.rel]
        try:
            winner_sha = winner.checksum(plan.rel)
            loser_sha = loser.checksum(plan.rel)
        except EndpointError as e:
            outcome.status = "unresolved"
            outcome.error = f"pre-mutation re-hash failed: {e}"
            report.error = f"stopped before mutating {plan.rel!r}: {e}"
            return
        if winner_sha != plan.winner_sha or loser_sha != plan.loser_sha:
            outcome.status = "unresolved"
            outcome.error = "changed since preflight (race detected)"
            report.error = (
                f"race detected: {plan.rel!r} changed between preflight and"
                " mutation; stopped without touching it"
            )
            return

        staging = staging_root / plan.rel
        try:
            winner.fetch_to(plan.rel, staging)
            if sha256_file(staging) != plan.winner_sha:
                raise EndpointError(
                    f"staged copy of {plan.rel!r} failed checksum verification"
                )
            loser.install(plan.rel, staging, plan.winner_sha)
            staging.unlink(missing_ok=True)
        except (EndpointError, OSError) as e:
            outcome.status = "unresolved"
            outcome.error = f"transfer failed: {e}"
            report.error = f"stopped at {plan.rel!r}: {e}"
            return
        outcome.status = "completed"
        outcome.mutation = "succeeded"


def _verify_conflicts_cleared(
    sync: MutagenSync, session: str, report: ResolveReport, idle_timeout: int
) -> None:
    try:
        sync.flush(session)
        if not sync.wait_idle(session, timeout=idle_timeout):
            report.error = (
                f"sync session {session!r} did not return to idle after flush"
            )
            return
        remaining = {normalize_conflict(p) for p in sync.conflicted_paths(session)}
    except MutagenError as e:
        report.error = f"conflict re-check failed: {e}"
        return
    for rel, outcome in report.outcomes.items():
        if outcome.status != "completed":
            continue
        outcome.conflict_cleared = rel not in remaining
        if not outcome.conflict_cleared:
            outcome.status = "unresolved"
            outcome.error = "still conflicted after flush"
    still = [r for r, o in report.outcomes.items() if o.conflict_cleared is False]
    if still:
        report.error = f"still conflicted after resolution flush: {', '.join(still)}"


def _write_manifest(
    report: ResolveReport, local_ep: Endpoint, cluster_ep: Endpoint
) -> None:
    assert report.run_dir is not None
    report.run_dir.mkdir(parents=True, exist_ok=True)
    plan_by_rel = {p.rel: p for p in report.plans}
    entries = []
    for rel, outcome in report.outcomes.items():
        plan = plan_by_rel[rel]
        entries.append(
            {
                "path": rel,
                "status": outcome.status,
                "mutation": outcome.mutation,
                "conflict_cleared": outcome.conflict_cleared,
                "original": {
                    "winner": {
                        "side": plan.source.value,
                        "size": plan.winner_size,
                        "sha256": plan.winner_sha,
                    },
                    "loser": {
                        "side": plan.loser_side,
                        "size": plan.loser_size,
                        "sha256": plan.loser_sha,
                    },
                },
                "backup": {
                    "path": str(plan.backup_path),
                    "sha256": outcome.backup_sha,
                },
            }
        )
    manifest = {
        "session": report.session,
        "source": report.source.value,
        "endpoints": {
            "local": local_ep.describe(),
            "cluster": cluster_ep.describe(),
        },
        "started_utc": report.started_utc,
        "finished_utc": report.finished_utc,
        "error": report.error,
        "paths": entries,
    }
    dest = report.run_dir / "manifest.json"
    tmp = dest.with_name("manifest.json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2) + "\n")
    os.replace(tmp, dest)
