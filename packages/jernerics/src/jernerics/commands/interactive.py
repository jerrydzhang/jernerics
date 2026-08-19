import json
import shlex
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from jernerics.backend.host import SSHHost
from jernerics.backend.project_sync import ProjectSync
from jernerics.backend.slurm.interactive import (
    InteractiveSession,
    format_interactive_script,
)
from jernerics.config import (
    ConfigNotFound,
    ExitCode,
    InteractiveConfig,
    SlurmConfig,
    find_pyproject_dir,
    get_project_name,
    load_backend_config,
)
from jernerics.sync.mutagen_sync import (
    MutagenError,
    MutagenNotFound,
    MutagenSync,
    SessionInfo,
    is_converged,
    is_idle,
    session_name,
)
from jernerics.sync.resolve import (
    PathPlan,
    ResolveError,
    ResolveReport,
    SourceSide,
    resolve_conflicts,
)


def _build_interactive_session(
    backend_name: str,
    *,
    time: str | None,
    gpus: int | None,
    partition: str | None,
    constraint: str | None,
) -> tuple[InteractiveSession, Path, str]:
    """Resolve config + CLI flags into an :class:`InteractiveSession`."""
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    try:
        config = load_backend_config(backend_name)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    if config.shared.type != "slurm" or not isinstance(config.backend, SlurmConfig):
        print(
            f"Error: 'interactive' requires a slurm backend; '{backend_name}'"
            f" is '{config.shared.type}'."
        )
        raise SystemExit(ExitCode.CONFIG_ERROR)

    if not config.shared.host:
        print("Error: 'interactive' requires an SSH host (none configured).")
        raise SystemExit(ExitCode.CONFIG_ERROR)

    project_name = get_project_name(project_dir)
    host = SSHHost(config.shared.host)
    slurm = config.backend
    interactive = config.interactive or InteractiveConfig()

    remote_dir = (
        config.shared.remote_dir.replace("~", host.home)
        .replace("{project_name}", project_name)
        .replace("{project-name}", project_name)
    )
    cache_host = (
        config.shared.cache_dir.replace("~", host.home)
        .replace("{project_name}", project_name)
        .replace("{project-name}", project_name)
        if config.shared.cache_dir
        else f"{host.home}/.cache/jernerics"
    )

    login_target = config.shared.host
    user = login_target.split("@", 1)[0] if "@" in login_target else None

    session = InteractiveSession(
        host=host,
        job_name=f"jernerics-interactive-{project_name}",
        remote_dir=remote_dir,
        container_image=f"{remote_dir}/container.sif",
        cache_host=cache_host,
        partition=partition or interactive.partition or slurm.partition,
        time_limit=time or interactive.time or slurm.time or "4:00:00",
        gpus=gpus if gpus is not None else interactive.gpus,
        mem=interactive.mem or slurm.mem or "16G",
        cpus=interactive.cpus if interactive.cpus is not None else slurm.cpus,
        constraint=constraint or interactive.constraint,
        login_target=login_target,
        user=user,
    )
    return session, project_dir, project_name


def _warn_sync_orphans(project_name: str, *, alive: bool) -> None:
    """Warn about jernerics sync sessions whose allocation is gone."""
    if not MutagenSync.available():
        return
    alive_names = {session_name(project_name)} if alive else set()
    try:
        orphans = MutagenSync().find_orphans(alive_names=alive_names)
    except MutagenError as e:
        print(f"Warning: could not list sync sessions ({e}).")
        return
    if not orphans:
        return
    print(f"Warning: {len(orphans)} stale sync session(s) with no live allocation:")
    for orphan in orphans:
        print(f"  - {orphan.name}")
    print("  Remove with: mutagen sync terminate <name>")


def _terminate_interactive_sync(project_name: str) -> None:
    """Stop the mutagen sync session for ``project_name`` (idempotent)."""
    if not MutagenSync.available():
        return
    name = session_name(project_name)
    try:
        MutagenSync().terminate(name)
        print(f"Stopped code sync ({name}).")
    except MutagenError as e:
        print(f"Warning: could not stop sync session {name} ({e}).")


def _oneshot_sync(session: InteractiveSession, project_dir: Path) -> None:
    """Push project source to the remote once via tar/scp (mutagen fallback)."""
    try:
        ProjectSync(session.host, session.remote_dir).sync_project(project_dir)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"Warning: one-shot project sync failed ({e}).")


def _warn_sync_unhealthy(sync: MutagenSync, name: str, record: SessionInfo) -> None:
    """Report an unhealthy or conflicted sync session (report-only).

    Never restarts or overwrites: restarting cannot clear conflicts, and a
    one-way push would clobber one side. Surfaces the state and lets the user
    resolve it.
    """
    print(f"Warning: code sync session {name} is not healthy:")
    print(
        f"  status: {record.status}, alpha connected: {record.alpha_connected},"
        f" beta connected: {record.beta_connected}, conflicts: {record.conflicts}"
    )
    if record.conflicts == 0:
        return
    try:
        paths = sync.conflicted_paths(name)
    except MutagenError as e:
        print(f"  Could not list conflicted paths ({e}).")
        return
    for path in paths:
        print(f"  {path}")
    print("Conflicted files do not propagate in either direction (two-way-safe).")
    print("Inspect with: mutagen sync list --long")
    print(
        "Resolve by making both sides agree (e.g. scp the winner to the other"
        " side), then re-run this command."
    )


def _ensure_interactive_sync(
    session: InteractiveSession,
    project_dir: Path,
    project_name: str,
    *,
    reconnect: bool,
) -> None:
    """Start or resume continuous code sync before attaching to the shell.

    A fresh allocation creates and waits on a new sync session. A reconnect
    leaves a still-live converged session in place and restarts a dead one;
    an unhealthy or conflicted session is reported, never restarted or
    overwritten. When mutagen is missing or fails, falls back to a single
    ProjectSync push so the remote starts with current source (no continuous
    sync).
    """
    remote_host = session.login_target
    if not remote_host:
        return

    if not MutagenSync.available():
        print("Warning: mutagen not found; using one-shot project sync.")
        print("         Code edits will not propagate until you re-run this command.")
        _oneshot_sync(session, project_dir)
        return

    name = session_name(project_name)
    sync = MutagenSync()
    try:
        record = next((s for s in sync.list_sessions() if s.name == name), None)
        if reconnect and record is not None:
            if is_converged(record):
                print(f"Continuous code sync already running ({name}).")
            else:
                # Restarting cannot clear conflicts and would lose the sync
                # baseline; a one-shot fallback would overwrite the remote.
                _warn_sync_unhealthy(sync, name, record)
            return
        if record is not None:
            # No live allocation backs it (fresh allocation path): the lingering
            # session is stale. Clear it so ``create`` does not collide on name.
            print(f"Replacing stale sync session ({name})...")
            sync.terminate(name)
        elif reconnect:
            print(f"Sync session {name} was lost; restarting...")
        else:
            print(f"Starting continuous code sync ({name})...")
        sync.start(project_dir, remote_host, session.remote_dir, name=name)
        print("Code sync is live: edits propagate in both directions within seconds.")
    except (MutagenError, MutagenNotFound) as e:
        print(f"Warning: continuous sync unavailable ({e}); using one-shot sync.")
        _oneshot_sync(session, project_dir)
        return
    try:
        record = next((s for s in sync.list_sessions() if s.name == name), None)
    except (MutagenError, MutagenNotFound):
        return
    if record is not None and record.conflicts > 0:
        _warn_sync_unhealthy(sync, name, record)


def _interactive_connect(
    session: InteractiveSession,
    node: str,
    backend_name: str,
    *,
    project_dir: Path,
    project_name: str,
    reconnect: bool = False,
    job_id: str | None = None,
) -> None:
    """Ensure code sync is live, then attach to the allocation."""
    _ensure_interactive_sync(session, project_dir, project_name, reconnect=reconnect)
    session.connect(node)
    if job_id is None:
        info = session.find_existing()
        job_id = info.job_id if info else "?"
    print()
    print(f"Disconnected from {node}. The allocation (job {job_id}) is still running.")
    print(f"  Reconnect:  jernerics interactive start --backend {backend_name}")
    print(f"  End:        jernerics interactive stop --backend {backend_name}")


def start(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    time: Annotated[
        str | None, typer.Option("--time", help="Walltime, e.g. 4:00:00")
    ] = None,
    gpus: Annotated[
        int | None, typer.Option("--gpus", help="Number of GPUs to allocate")
    ] = None,
    partition: Annotated[
        str | None, typer.Option("--partition", help="SLURM partition")
    ] = None,
    constraint: Annotated[
        str | None, typer.Option("--constraint", help="SLURM constraint, e.g. a100")
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Show sbatch script and ssh command without running"
        ),
    ] = False,
) -> None:
    """Open a container shell on an allocated GPU node.

    Allocates a GPU via a reservation job that survives SSH disconnect, then
    drops you into an apptainer shell inside the container at /work. Continuous
    code sync (mutagen) mirrors edits in both directions while the allocation
    runs. Re-run to reconnect to an existing allocation; stop it with
    ``jernerics interactive stop``.

    Process persistence (tmux, screen) is left to the user.
    """
    session, project_dir, project_name = _build_interactive_session(
        backend_name, time=time, gpus=gpus, partition=partition, constraint=constraint
    )

    if dry_run:
        script = format_interactive_script(
            job_name=session.job_name,
            partition=session.partition,
            time_limit=session.time_limit,
            mem=session.mem,
            cpus=session.cpus,
            gpus=session.gpus,
            constraint=session.constraint,
        )
        print("=== SBATCH SCRIPT ===")
        print(script)
        print()
        print("=== SSH (after allocation; NODE is the compute host) ===")
        print(" ".join(shlex.quote(a) for a in session.ssh_argv("NODE")))
        return

    existing = session.find_existing()

    _warn_sync_orphans(project_name, alive=existing is not None)

    if existing is not None:
        if existing.state == "RUNNING" and existing.node:
            print(f"Reconnecting to job {existing.job_id} on {existing.node}...")
            _interactive_connect(
                session,
                existing.node,
                backend_name,
                project_dir=project_dir,
                project_name=project_name,
                reconnect=True,
            )
            return
        print(
            f"Existing session job {existing.job_id} is {existing.state};"
            " waiting for it to start..."
        )
        node = session.wait_for_running(existing.job_id)
        print(f"Allocation running on {node}.")
        _interactive_connect(
            session,
            node,
            backend_name,
            project_dir=project_dir,
            project_name=project_name,
            reconnect=True,
        )
        return

    readiness = session.host.run(["test", "-f", session.container_image], check=False)
    if readiness.returncode != 0:
        print(f"Error: container not found at {session.container_image}.")
        print("  Run 'jernerics backend build --backend <name>' first.")
        raise SystemExit(ExitCode.CONTAINER_ERROR) from None

    session.host.mkdir(session.cache_host)
    print(
        f"Submitting interactive allocation ({session.gpus} GPU,"
        f" partition {session.partition}, {session.time_limit})..."
    )
    job_id = session.submit()
    print(f"Submitted job {job_id}. Waiting for it to start...")
    node = session.wait_for_running(job_id)
    print(f"Allocation running on {node}.")
    _interactive_connect(
        session,
        node,
        backend_name,
        project_dir=project_dir,
        project_name=project_name,
        reconnect=False,
        job_id=job_id,
    )


def stop(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
) -> None:
    """Tear down an interactive session and stop its code sync."""
    session, _, project_name = _build_interactive_session(
        backend_name, time=None, gpus=None, partition=None, constraint=None
    )

    existing = session.find_existing()
    if existing is None:
        print(f"No active interactive session for backend '{backend_name}'.")
        return
    _terminate_interactive_sync(project_name)
    session.end()
    print(f"Cancelled interactive job {existing.job_id} (was {existing.state}).")


def sync_status(
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
) -> None:
    """Report the state of this project's interactive code-sync session.

    Read-only: never creates, restarts, terminates, or flushes the mutagen
    session — a missing, disconnected, syncing, or conflicted session is data,
    not an error. Exits nonzero only when the config or mutagen itself
    prevents inspection.
    """
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)
    try:
        load_backend_config(backend_name)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_name = get_project_name(project_dir)
    name = session_name(project_name)

    if not MutagenSync.available():
        print("Error: mutagen not found on PATH; cannot inspect sync status.")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    sync = MutagenSync()
    try:
        record = next((s for s in sync.list_sessions() if s.name == name), None)
        paths = sync.conflicted_paths(name) if record and record.conflicts else []
    except (MutagenError, MutagenNotFound) as e:
        print(f"Error: could not inspect sync session {name} ({e}).")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    report = {
        "project": project_name,
        "backend": backend_name,
        "session": name,
        "exists": record is not None,
        "status": record.status if record is not None else None,
        "local_connected": record.alpha_connected if record is not None else None,
        "cluster_connected": record.beta_connected if record is not None else None,
        "idle": is_idle(record) if record is not None else False,
        "converged": is_converged(record) if record is not None else False,
        "conflicts": record.conflicts if record is not None else 0,
        "conflicted_paths": paths,
    }

    if json_output:
        print(json.dumps(report, indent=2))
        return

    if record is None:
        print(f"Sync session {name} not found (project '{project_name}').")
        print(f"Start one with: jernerics interactive start --backend {backend_name}")
        return

    print(f"Session:   {name}")
    print(f"Status:    {record.status}")
    print(
        f"Local:     {'connected' if record.alpha_connected else 'disconnected'}"
        f" ({record.alpha_path})"
    )
    print(
        f"Cluster:   {'connected' if record.beta_connected else 'disconnected'}"
        f" ({record.beta_path})"
    )
    print(f"Idle:      {'yes' if report['idle'] else 'no'}")
    print(f"Converged: {'yes' if report['converged'] else 'no'}")
    print(f"Conflicts: {record.conflicts}")
    if record.conflicts:
        print("Conflicted paths:")
        for path in paths:
            print(f"  {path}")
        print("Conflicted files do not propagate in either direction (two-way-safe).")
        print("Inspect with: mutagen sync list --long")


def _print_resolution_preview(plans: list[PathPlan], run_dir: Path) -> None:
    """Render the pre-mutation preview: one block per selected path."""
    print(f"Backup run directory: {run_dir}")
    for plan in plans:
        assert plan.backup_path is not None
        print(f"  {plan.rel}  ({plan.direction}, {plan.winner_size} bytes)")
        print(f"    winner sha256 ({plan.source.value}): {plan.winner_sha}")
        print(f"    loser sha256 ({plan.loser_side}): {plan.loser_sha}")
        print(f"    loser backup: {plan.backup_path}")


def _print_resolution_report(report: ResolveReport) -> None:
    if report.error is not None:
        print(f"Error: {report.error}")
    if report.completed:
        print(f"Completed ({len(report.completed)}):")
        for rel in report.completed:
            print(f"  {rel}")
    if report.unresolved:
        print(f"Unresolved ({len(report.unresolved)}):")
        for rel in report.unresolved:
            outcome = report.outcomes[rel]
            detail = f" — {outcome.error}" if outcome.error else ""
            print(f"  {rel}{detail}")
    if report.untouched:
        print(f"Untouched ({len(report.untouched)}):")
        for rel in report.untouched:
            print(f"  {rel}")
    if report.run_dir is not None:
        print(f"Backups (never auto-deleted): {report.run_dir}")


def sync_resolve(
    paths: Annotated[
        list[Path],
        typer.Argument(help="Conflicted relative paths to resolve"),
    ],
    backend_name: Annotated[
        str, typer.Option("--backend", "-b", help="Backend name from config")
    ],
    source: Annotated[SourceSide, typer.Option("--from", help="Which side wins")],
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Preview only; no backup, no change")
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Proceed without prompting (required when noninteractive)"
        ),
    ] = False,
) -> None:
    """Resolve sync conflicts by overwriting one side from the other.

    Safety: losers are backed up locally first, every transfer is checksum
    verified, and the session is flushed afterwards — the mutagen session
    itself is never restarted. One --from side wins for every listed PATH.
    """
    project_dir = find_pyproject_dir()
    if project_dir is None:
        print("Error: No pyproject.toml found. Run 'jernerics init' to create one.")
        raise SystemExit(ExitCode.CONFIG_ERROR)
    try:
        load_backend_config(backend_name)
    except ConfigNotFound as e:
        print(f"Error: {e}")
        raise SystemExit(ExitCode.CONFIG_ERROR) from None

    project_name = get_project_name(project_dir)
    name = session_name(project_name)

    if not MutagenSync.available():
        print("Error: mutagen not found on PATH; cannot resolve conflicts.")
        raise SystemExit(ExitCode.GENERAL_ERROR)

    try:
        report = resolve_conflicts(
            MutagenSync(),
            name,
            [str(p) for p in paths],
            source,
            project_name,
            dry_run=dry_run,
            assume_yes=yes,
            confirm=typer.confirm,
            on_plans=_print_resolution_preview,
        )
    except ResolveError as e:
        if str(e) == "declined":
            print("Aborted; nothing was changed.")
        else:
            print(f"Error: {e}")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None
    except (MutagenError, MutagenNotFound) as e:
        print(f"Error: could not inspect sync session {name} ({e}).")
        raise SystemExit(ExitCode.GENERAL_ERROR) from None

    if report.dry_run:
        print("Dry run: no backups written, nothing changed.")
        return
    _print_resolution_report(report)
    if not report.ok:
        raise SystemExit(ExitCode.GENERAL_ERROR)
    print(f"Resolved {len(report.completed)} path(s); conflicts cleared.")


def register(app: typer.Typer) -> None:
    group = typer.Typer(help="Interactive GPU shells on a backend")
    group.command("start")(start)
    group.command("stop")(stop)
    sync_group = typer.Typer(help="Inspect and resolve interactive code sync")
    sync_group.command("status")(sync_status)
    sync_group.command("resolve")(sync_resolve)
    group.add_typer(sync_group, name="sync")
    app.add_typer(group, name="interactive")
