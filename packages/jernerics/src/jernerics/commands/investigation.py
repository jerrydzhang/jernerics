import json
import uuid
from enum import Enum
from typing import Annotated

import typer
from jernerics_schema import encode_selection, materialize_selection
from rich.console import Console
from rich.table import Table

from jernerics.commands.tracking import _fail, _open_tracking_client
from jernerics.observability.render import _format_relative
from jernerics.tracking import ProjectHandle, TrackingClientError


class InvestigationRefError(Exception):
    """An investigation reference could not be resolved to one record."""


class MembersAction(str, Enum):
    SET = "set"
    ADD = "add"
    REMOVE = "remove"


_JSON = Annotated[bool, typer.Option("--json", help="Output as JSON")]
_REF = Annotated[str, typer.Argument(help="Investigation id or project-unique name")]


def _resolve_investigation_id(handle: ProjectHandle, ref: str) -> str:
    """The investigation id for a raw uuid, else a project-unique name."""
    try:
        return str(uuid.UUID(ref))
    except ValueError:
        pass
    records = handle.investigations(include_archived=True)
    named = [record for record in records if record.name == ref]
    if len(named) == 1:
        return str(named[0].id)
    if not named:
        known = ", ".join(sorted(record.name for record in records))
        hint = f" (known: {known})" if known else ""
        raise InvestigationRefError(f"no investigation named {ref!r}{hint}")
    candidates = ", ".join(sorted(str(record.id) for record in named))
    raise InvestigationRefError(
        f"investigation name {ref!r} is ambiguous: {candidates}"
    )


def _render_list(payload: list[dict], console: Console) -> None:
    if not payload:
        console.print("No investigations found.")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("NAME")
    table.add_column("FACTOR")
    table.add_column("OUTCOME")
    table.add_column("REPLICATE")
    table.add_column("MEMBERS", justify="right")
    table.add_column("ARCHIVED", justify="right")
    table.add_column("UPDATED", justify="right")
    for record in payload:
        table.add_row(
            record["name"],
            record["factor"],
            record["outcome"],
            record["replicate_factor"] or "-",
            str(len(record["members"])),
            "yes" if record["archived_ns"] is not None else "-",
            _format_relative(record["updated_ns"]),
        )
    console.print(table)


def _render_show(payload: dict, console: Console) -> None:
    investigation = payload["investigation"]
    coverage = payload["coverage"]
    console.print(f"[bold]{investigation['name']}[/bold] ({investigation['id'][:8]})")
    replicate = investigation["replicate_factor"] or "-"
    console.print(
        f"  factor: {investigation['factor']}    "
        f"outcome: {investigation['outcome']}    replicate: {replicate}"
    )
    console.print(
        f"  members: {coverage['members']}    with outcome: {coverage['with_outcome']}"
        f"    completed: {coverage['completed']}    invalid: {coverage['invalid']}"
    )
    console.print(f"  last activity: {_format_relative(coverage['last_activity_ns'])}")
    if investigation["archived_ns"] is not None:
        console.print("  archived")
    for sweep_id in investigation["members"]:
        console.print(f"  {sweep_id}")
    console.print(f"  selection token: {payload['selection']['token']}")


def _render_preview(payload: dict, console: Console) -> None:
    console.print(
        f"Preview over {payload['member_count']} candidate sweeps "
        f"in {payload['project']!r}"
    )
    console.print("\nFactors:")
    if payload["factors"]:
        table = Table(show_header=True, header_style="bold")
        table.add_column("KIND")
        table.add_column("NAME")
        table.add_column("MEMBERS", justify="right")
        for factor in payload["factors"]:
            table.add_row(factor["kind"], factor["name"], str(factor["members"]))
        console.print(table)
    else:
        console.print("  none")
    console.print("\nOutcomes:")
    if payload["outcomes"]:
        table = Table(show_header=True, header_style="bold")
        table.add_column("KEY")
        table.add_column("MEMBERS", justify="right")
        for outcome in payload["outcomes"]:
            table.add_row(outcome["key"], str(outcome["members"]))
        console.print(table)
    else:
        console.print("  none")
    console.print("\nWarnings:")
    if payload["warnings"]:
        for warning in payload["warnings"]:
            console.print(f"  {warning['kind']}: {warning['detail']}")
    else:
        console.print("  none")


def list_investigations(
    include_archived: Annotated[
        bool,
        typer.Option("--include-archived", help="Also list archived investigations"),
    ] = False,
    json_output: _JSON = False,
) -> None:
    """List this project's investigations."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            records = handle.investigations(include_archived=include_archived)
        except TrackingClientError as e:
            _fail(str(e))
        payload = [record.model_dump(mode="json") for record in records]
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    _render_list(payload, Console())


def show(ref: _REF, json_output: _JSON = False) -> None:
    """Show one investigation with coverage facts and its Selection."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            detail = handle.investigation(_resolve_investigation_id(handle, ref))
        except (TrackingClientError, InvestigationRefError) as e:
            _fail(str(e))
        selection = materialize_selection(detail.investigation)
        payload = detail.model_dump(mode="json")
    payload["selection"] = {
        "project": selection.project,
        "sweeps": [str(sweep_id) for sweep_id in selection.sweeps or ()],
        "token": encode_selection(selection),
    }
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    _render_show(payload, Console())


def preview(
    sweep_ids: Annotated[
        list[str] | None, typer.Argument(help="Candidate member sweep ids")
    ] = None,
    json_output: _JSON = False,
) -> None:
    """Show factor/outcome coverage a membership would have."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            result = handle.investigation_preview(sweep_ids or [])
        except TrackingClientError as e:
            _fail(str(e))
        payload = result.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    _render_preview(payload, Console())


def create(
    name: Annotated[str, typer.Argument(help="Project-unique investigation name")],
    factor: Annotated[str, typer.Option("--factor", help="Primary comparison factor")],
    outcome: Annotated[str, typer.Option("--outcome", help="Outcome value key")],
    sweep_ids: Annotated[
        list[str] | None, typer.Argument(help="Initial member sweep ids")
    ] = None,
    replicate_factor: Annotated[
        str | None,
        typer.Option("--replicate-factor", help="Optional replicate factor"),
    ] = None,
    json_output: _JSON = False,
) -> None:
    """Create (or idempotently re-create) a named investigation."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            record = handle.create_investigation(
                name,
                factor,
                outcome,
                members=sweep_ids or [],
                replicate_factor=replicate_factor,
            )
        except TrackingClientError as e:
            _fail(str(e))
        payload = record.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    console = Console()
    console.print(
        f"Created investigation '{record.name}' ({len(record.members)} member sweeps)"
    )


def members(
    action: Annotated[
        MembersAction, typer.Argument(help="Membership operation: set, add, or remove")
    ],
    ref: _REF,
    sweep_ids: Annotated[
        list[str] | None, typer.Argument(help="Member sweep ids")
    ] = None,
    json_output: _JSON = False,
) -> None:
    """Set, add, or remove member sweeps of an investigation."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            investigation_id = _resolve_investigation_id(handle, ref)
            mutate = {
                MembersAction.SET: handle.set_investigation_members,
                MembersAction.ADD: handle.add_investigation_members,
                MembersAction.REMOVE: handle.remove_investigation_members,
            }[action]
            record = mutate(investigation_id, sweep_ids or [])
        except (TrackingClientError, InvestigationRefError) as e:
            _fail(str(e))
        payload = record.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    verb = {
        MembersAction.SET: "Set members of",
        MembersAction.ADD: "Added members to",
        MembersAction.REMOVE: "Removed members from",
    }[action]
    Console().print(f"{verb} '{record.name}' (now {len(record.members)} member sweeps)")


def archive(ref: _REF, json_output: _JSON = False) -> None:
    """Archive an investigation (hidden from the default list)."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            record = handle.archive_investigation(
                _resolve_investigation_id(handle, ref)
            )
        except (TrackingClientError, InvestigationRefError) as e:
            _fail(str(e))
        payload = record.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    Console().print(f"Archived '{record.name}'")


def restore(ref: _REF, json_output: _JSON = False) -> None:
    """Restore an archived investigation."""
    client, project_name = _open_tracking_client()
    with client:
        handle = client.project(project_name)
        try:
            record = handle.restore_investigation(
                _resolve_investigation_id(handle, ref)
            )
        except (TrackingClientError, InvestigationRefError) as e:
            _fail(str(e))
        payload = record.model_dump(mode="json")
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    Console().print(f"Restored '{record.name}'")


def register(app: typer.Typer) -> None:
    group = typer.Typer(help="Organize sweeps into named investigations")
    group.command("list")(list_investigations)
    group.command("show")(show)
    group.command("preview")(preview)
    group.command("create")(create)
    group.command("members")(members)
    group.command("archive")(archive)
    group.command("restore")(restore)
    app.add_typer(group, name="investigation")
