"""Navigation, project picker, selection tray, and polling callbacks.

Routing model: the project catalog, the persistent workspace, the
investigation pages, and the per-sweep page are routed by URL; the
sweep page refreshes its region from the view document's ``scope``
picks and the poll interval while the sweep is incomplete.
"""

import hashlib
import json
import time
from typing import Any

import dash
from dash import ALL, Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate

from . import analysis, artifacts, components, figures, layout, sweep, workspace
from .components import Error, short_id
from .routes import ROUTES_BASE, parse_route
from .service import (
    CurationRejectedError,
    CurationUnavailableError,
    DashboardService,
)


def overlay_axis_control(triggered: Any) -> str | None:
    """Which overlay axis control fired: the control name after the
    static ``analysis-overlay-`` prefix, else ``None``."""
    prop = next((str(prop) for prop in triggered or ()), "")
    control = prop.rsplit(".", 1)[0].removeprefix("analysis-overlay-")
    return control if control in {"scale", "range", "min", "max", "reset"} else None


def _json_default(value: Any) -> Any:
    """JSON stand-in for otherwise unserializable values; Dash component
    trees serialize through their plotly JSON, everything else by str."""
    if hasattr(value, "to_plotly_json"):
        return value.to_plotly_json()
    return str(value)


def _figure_payload(figure: Any) -> Any:
    """Store-safe form of a plotly figure (the store needs plain JSON)."""
    return figure.to_plotly_json() if hasattr(figure, "to_plotly_json") else figure


def _content_digest(*parts: Any) -> str:
    """Stable digest of callback outputs, for skip-when-unchanged guards."""
    payload = json.dumps(parts, sort_keys=True, default=_json_default)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def pattern_trigger(context: Any) -> tuple[str | None, str]:
    """(metric, control) of the pattern component that fired a MATCH
    callback; ``(None, "")`` when nothing did. Pattern prop ids carry
    the resolved id as a JSON object before the final ``.prop``."""
    triggered = context.triggered
    if not triggered:
        return None, ""
    id_json = triggered[0]["prop_id"].rsplit(".", 1)[0]
    try:
        resolved = json.loads(id_json)
    except ValueError:
        return None, ""
    if not isinstance(resolved, dict) or len(resolved) != 1:
        return None, ""
    control, metric = next(iter(resolved.items()))
    return str(metric), str(control)


def pattern_input_value(
    inputs_list: Any, entry: int, pattern_key: str, metric: str
) -> Any:
    """The current value of one ALL-pattern input, located by its
    resolved component id (ALL inputs arrive as lists)."""
    items = inputs_list[entry] if isinstance(inputs_list, list) else []
    for item in items or []:
        component_id = item.get("id") if isinstance(item, dict) else None
        if isinstance(component_id, dict) and component_id.get(pattern_key) == metric:
            return item.get("value")
    return None


def page_content(
    pathname: str | None,
    service: DashboardService,
    *,
    now_ns: int | None = None,
    workspace_state_doc: dict | None = None,
    view_doc: dict | None = None,
    search: str | None = None,
) -> tuple[Any, bool]:
    """(page, poll enabled) for a URL, with live data.

    ``poll enabled`` is True only while the workspace's work is
    incomplete: any sweep still running or the focused object open.
    """
    spec = parse_route(pathname)
    now = time.time_ns() if now_ns is None else now_ns
    if spec.kind == "project":
        return layout.project_page(service.project_catalog(), now), False
    if spec.kind == "workspace":
        project = spec.object_id or ""
        summaries = service.sweep_overview(project)
        polls = any(summary.incomplete for summary in summaries)
        state = workspace_state(workspace_state_doc, project)
        return (
            workspace.workspace_page(
                project,
                sort=state["sort"],
                quick=state["quick"],
                filters=state["filters"],
            ),
            polls,
        )
    if spec.kind == "investigation":
        investigation_id = spec.sub_id or ""
        try:
            page = workspace.investigation_page(
                service,
                spec.object_id or "",
                investigation_id,
                search=search,
            )
        except CurationUnavailableError as error:
            return components.Empty(str(error)), False
        except CurationRejectedError:
            return layout.missing_object_page("investigation", investigation_id), False
        return page, False
    if spec.kind == "investigation-edit":
        try:
            page = workspace.investigation_edit_page(
                service,
                spec.object_id or "",
                spec.sub_id,
                analysis.seed_sweeps_from_search(search),
            )
        except CurationUnavailableError as error:
            return components.Empty(str(error)), False
        except CurationRejectedError:
            return layout.missing_object_page("investigation", spec.sub_id or ""), False
        return page, False
    if spec.kind == "sweep":
        sweep_id = spec.sub_id or ""
        found = sweep.page(
            service,
            spec.object_id or "",
            sweep_id,
            sweep.via_from_search(search),
            now,
            picked_families=set(
                ((view_doc or {}).get("scope") or {}).get("families") or ()
            ),
        )
        if found is None:
            return layout.missing_object_page("sweep", sweep_id), False
        return found, service.sweep_incomplete(sweep_id)
    if spec.kind == "artifact":
        view = service.artifact_view(spec.object_id or "")
        if view is None:
            return (
                layout.missing_object_page("artifact", spec.object_id or ""),
                False,
            )
        return artifacts.viewer_page(service, view, now), False
    return layout.not_found_page(pathname or ""), False


def is_initial() -> bool:
    """True inside a callback's initial call (nothing changed to fire
    it) — ``callback_context.triggered`` is falsy exactly then."""
    return not dash.callback_context.triggered


def pressed_props(context: Any) -> set[str]:
    """Prop ids of the controls a user actually pressed: a re-render
    remounts controls and re-fires their callbacks with click counts of
    None, which must never act (jernerics-gk6)."""
    return {
        str(_event_field(event, "prop_id"))
        for event in context.triggered or ()
        if _event_field(event, "value")
    }


def investigation_new_href(project: str, sweep_ids: list[str]) -> tuple[str, str]:
    """The editor URL target seeding a new investigation with the
    picked sweeps; jernerics-g5rw.8 consumes the ``?sweeps=`` token.
    Sweep ids are hyphenated hex, so the CSV needs no quoting."""
    unique = sorted(set(sweep_ids))
    return (
        f"{ROUTES_BASE}/project/{project}/investigation/new",
        "?sweeps=" + ",".join(unique),
    )


def project_options(projects: list[str]) -> list[dict[str, str]]:
    return [{"label": project, "value": project} for project in projects]


def tray_from_grid(rows: list[dict] | None, current: dict | None) -> dict:
    """Merge the browser's sweep checkbox selection into the scope
    group, keeping the analysis-side picks and include flags so a
    workspace pick survives focus edits."""
    return {
        **(current or analysis.default_scope_state()),
        "sweeps": sorted({str(row["sweep_id"]) for row in rows or []}),
    }


def overview_facts(
    service: DashboardService,
    project: str | None,
    tray: dict | None,
    overview_filter: str | None = None,
) -> dict[str, Any]:
    """Canonical overview facts: one stored-facts row per scoped sweep,
    the scope identity, and the active tile filter — never the rendered
    tree, so relative-time strings cannot churn the digest
    (jernerics-l4k)."""
    if not project:
        return {"project": None}
    picked = sorted(set((tray or {}).get("sweeps") or []))
    return {
        "project": project,
        "picked": picked,
        "overview_filter": overview_filter,
        "sweeps": sorted(
            [
                (
                    str(summary.sweep_id),
                    summary.name,
                    summary.state,
                    summary.health,
                    summary.started,
                    summary.terminal,
                    summary.active,
                    summary.quiet,
                    summary.stale,
                    summary.unknown,
                    summary.succeeded,
                    summary.failed,
                    summary.expected_trials,
                    summary.latest_submitted_ns,
                    summary.archived_ns,
                    summary.invalid_ns,
                    summary.invalid_reason,
                )
                for summary in service.sweep_overview(project)
                if not picked or summary.sweep_id in picked
            ]
        ),
    }


def overview_content(
    service: DashboardService,
    project: str | None,
    tray: dict | None,
    overview_filter: str | None = None,
) -> tuple[dict[str, Any], str]:
    """(facts, digest) of the workspace overview region; the digest
    covers stored facts only, never the rendered children."""
    facts = overview_facts(service, project, tray, overview_filter)
    return facts, _content_digest(facts)


def workspace_state(store: dict | None, project: str | None) -> dict:
    """Per-project browser controls state from the session store."""
    saved = (store or {}).get(project or "") or {}
    return {
        "quick": str(saved.get("quick") or ""),
        "filters": saved.get("filters") or None,
        "sort": saved.get("sort") or None,
        "overview_sort": saved.get("overview_sort") or None,
    }


def sort_from_columns(columns: list | None) -> list | None:
    """Sort entries (colId/sort) extracted from AG Grid column state."""
    entries = [
        {"colId": column["colId"], "sort": column["sort"]}
        for column in columns or []
        if isinstance(column, dict) and column.get("sort")
    ]
    return entries or None


def remember_workspace(
    current: dict | None, project: str | None, **fields: object
) -> dict | None:
    """Session store after one browser control edit; ``None`` when the
    per-project state is unchanged (only edited fields are
    authoritative, so a mounting control's echo cannot wipe the rest)."""
    state = workspace_state(current, project)
    updated = {**state, **fields}
    if updated == state:
        return None
    return {**(current or {}), project or "": updated}


_CURATION_VERBS = {
    "archive": "Archived",
    "invalid": "Marked invalid",
    "restore_validity": "Restored validity of",
    "restore": "Restored",
}


def run_curation(
    service: DashboardService, action: str, sweep_id: str, reason: str
) -> str:
    """One service mutation per action name; returns the sweep's label."""
    if action == "archive":
        return service.archive_sweep(sweep_id)
    if action == "invalid":
        return service.mark_sweep_invalid(sweep_id, reason)
    if action == "restore_validity":
        return service.restore_sweep_validity(sweep_id)
    return service.restore_sweep(sweep_id)


def apply_curation(
    service: DashboardService, action: str, sweep_ids: list[str], reason: str = ""
) -> tuple[bool, str]:
    """Run one curation action over ids deterministically; (all ok,
    report) with per-sweep failures named, never a false all-succeeded.
    Already-invalid sweeps are named in the report, never re-marked —
    a silent rewrite of reason or timestamp would read as success."""
    if action == "invalid" and not reason.strip():
        return (
            False,
            "Mark invalid requires a reason (1..500 characters after trimming).",
        )
    unchanged: list[str] = []
    if action == "invalid":
        pending = []
        for sweep_id in sorted(set(sweep_ids)):
            summary = service.sweep_curation_state(sweep_id)
            if summary is not None and summary.invalid:
                unchanged.append(service.sweep_label(sweep_id))
            else:
                pending.append(sweep_id)
    else:
        pending = sorted(set(sweep_ids))
    labels: list[str] = []
    failures: list[str] = []
    for sweep_id in pending:
        try:
            labels.append(run_curation(service, action, sweep_id, reason))
        except (CurationUnavailableError, CurationRejectedError) as error:
            failures.append(f"{service.sweep_label(sweep_id)}: {error}")
    verb = _CURATION_VERBS[action]
    parts = [f"{verb} {', '.join(labels)}"] if labels else []
    if unchanged:
        parts.append(f"already invalid, reason untouched: {', '.join(unchanged)}")
    report = "; ".join(parts) + "." if parts else ""
    if failures:
        prefix = f"{report} " if report else ""
        report = f"{prefix}Failed — {'; '.join(failures)}."
    return not failures, report


def _post_action_grid(
    service: DashboardService, project: str | None, view_doc: dict | None
) -> tuple[list[dict], str]:
    """Fresh browser rows plus the curation note, recomputed from the
    CURRENT scope tray and include flags — never a bare snapshot, so
    picked curated sweeps stay visible across a curation action."""
    scope = (view_doc or {}).get("scope") or {}
    rows = workspace.browser_sweep_rows(
        service.sweep_overview(project or ""),
        scope,
        include_archived=bool(scope.get("include_archived")),
        include_invalid=bool(scope.get("include_invalid")),
    )
    return rows, workspace.curation_note(rows)


def triggered_action(triggered: set[str], mapping: dict[str, str]) -> str | None:
    """The action name for the one triggered control, if any."""
    return next((action for prop, action in mapping.items() if prop in triggered), None)


def selected_failed_sweeps(values: list) -> list[str]:
    """Checked sweep ids from the failure view's per-group checklists;
    each checklist carries one option, so a non-empty value is one id."""
    return [str(item) for group in values or [] for item in group]


def mounted_failed_sweep_ids() -> list[str]:
    """Mounted failure-view checklist ids from the request's ALL input —
    the layout truth select-all writes must match, not a fresh read."""
    return [
        str(item["id"]["failed-sweep"])
        for slot in dash.callback_context.inputs_list or []
        for item in (slot if isinstance(slot, list) else [slot])
        if isinstance(item, dict)
        and isinstance(item.get("id"), dict)
        and "failed-sweep" in item["id"]
    ]


def _event_field(event: Any, name: str) -> Any:
    """One ``triggered`` entry field; Dash has shipped both dict and
    attribute event shapes."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def register_callbacks(app: dash.Dash, service: DashboardService) -> None:
    @app.callback(
        Output("page-container", "children"),
        Output("poll", "disabled"),
        Output("view-store", "data", allow_duplicate=True),
        Output("route-store", "data"),
        Output("overview-digest-store", "data"),
        Input("url", "pathname"),
        Input("poll", "n_intervals"),
        State("workspace-store", "data"),
        State("view-store", "data"),
        State("route-store", "data"),
        State("url", "search"),
        prevent_initial_call="initial_duplicate",
    )
    def _render_page(
        pathname: str | None,
        _tick: int | None,
        workspace_doc: dict | None,
        view_doc: dict | None,
        rendered_route: str | None,
        search: str | None,
    ):
        kind = parse_route(pathname).kind
        project = parse_route(pathname).object_id
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        ticked = "poll.n_intervals" in triggered
        # A url.search rewrite re-fires the pathname watcher with the
        # route unchanged; re-rendering would remount the workspace and
        # orphan every grid under it for nothing.
        if (
            not ticked
            and set(triggered) == {"url.pathname"}
            and (pathname == rendered_route)
        ):
            raise PreventUpdate
        if ticked and kind == "workspace":
            polls = any(
                summary.incomplete for summary in service.sweep_overview(project or "")
            )
            flip = analysis.auto_refresh_flip(view_doc, polls)
            return (
                no_update,
                not polls,
                no_update if flip is None else flip,
                no_update,
                no_update,
            )
        if ticked and kind == "investigation":
            # A tick must never remount an investigation page (that
            # would reset every region); its views own their refresh
            # through the poll input inside their callbacks. The tick
            # still owns the auto-refresh flip: once the member scope
            # turned terminal, the persisted intent clears.
            doc = view_doc or analysis.default_view_state()
            polls = (
                bool(doc.get("auto_refresh"))
                and doc.get("inv", {}).get("view") == "series"
            )
            if polls:
                try:
                    tray, _scoped = analysis.investigation_scope_state(
                        service.investigation_detail(
                            parse_route(pathname).sub_id or ""
                        ).investigation.members,
                        doc["inv"].get("member"),
                    )
                    polls = service.analysis_scope_incomplete(project or "", tray)
                except CurationRejectedError:
                    polls = False
            flip = analysis.auto_refresh_flip(view_doc, polls)
            return (
                no_update,
                not polls,
                no_update if flip is None else flip,
                no_update,
                no_update,
            )
        if ticked and kind == "sweep":
            polls = service.sweep_incomplete(parse_route(pathname).sub_id or "")
            return no_update, not polls, no_update, no_update, no_update
        page, polls = page_content(
            pathname,
            service,
            workspace_state_doc=workspace_state(workspace_doc, project),
            view_doc=view_doc,
            search=search,
        )
        # A rendered page remounts workspace-overview empty, so its
        # content digest from the previous mount is void.
        return page, not polls, no_update, pathname, None

    @app.callback(
        Output("poll", "disabled", allow_duplicate=True),
        Output("poll-gate-facts-store", "data"),
        Input("url", "pathname"),
        Input("view-store", "data"),
        State("project-store", "data"),
        State("poll-gate-facts-store", "data"),
        prevent_initial_call=True,
    )
    def _gate_workspace_poll(
        pathname: str | None,
        view_doc: dict | None,
        project: str | None,
        facts: dict | None,
    ):
        # The router only fires on navigation and ticks; view edits must
        # re-evaluate the interval themselves — but only when a fact the
        # gate consumes actually changed, never on every view-store write.
        doc = view_doc or analysis.default_view_state()
        desired = {
            "project": project,
            "scope": analysis.scope_dims(doc.get("scope")),
            "auto_refresh": doc.get("auto_refresh"),
            "inv": doc.get("inv"),
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if (
            "url.pathname" not in triggered
            and (facts or {}).get("project") == desired["project"]
            and (facts or {}).get("scope") == desired["scope"]
            and (facts or {}).get("auto_refresh") == desired["auto_refresh"]
            and (facts or {}).get("inv") == desired["inv"]
        ):
            raise PreventUpdate
        kind = parse_route(pathname).kind
        if kind == "investigation":
            # The investigation Series view polls only while its own
            # auto-refresh intent is on and the member scope still has
            # incomplete work.
            if not (project and doc.get("auto_refresh")):
                return True, desired
            spec = parse_route(pathname)
            try:
                members = service.investigation_detail(
                    spec.sub_id or ""
                ).investigation.members
            except CurationRejectedError:
                return True, desired
            tray, _scoped = analysis.investigation_scope_state(
                members,
                (doc.get("inv") or {}).get("member"),
            )
            return (
                not service.analysis_scope_incomplete(project, tray),
                desired,
            )
        if kind == "sweep":
            sweep_id = parse_route(pathname).sub_id or ""
            return (not service.sweep_incomplete(sweep_id), desired)
        if kind != "workspace":
            raise PreventUpdate
        scope_open = bool(project) and service.analysis_scope_incomplete(
            project, doc.get("scope")
        )
        return (
            not (analysis.auto_refresh_polls(service, project, view_doc) or scope_open),
            desired,
        )

    app.clientside_callback(
        """
        function(active) {
            const display = (value) => ({display: active === value ? "block" : "none"});
            return [display("overview"), display("investigations"),
                    display("exceptions")];
        }
        """,
        Output("workspace-overview", "style"),
        Output("workspace-investigations", "style"),
        Output("workspace-exceptions", "style"),
        Input({"analysis-tabs": ALL}, "value"),
    )

    @app.callback(
        Output("project-picker", "options"),
        Input("url", "pathname"),
    )
    def _load_projects(pathname: str | None):
        if pathname is None:
            raise PreventUpdate
        return project_options(service.projects())

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input("project-picker", "value"),
        State("route-store", "data"),
        prevent_initial_call=True,
    )
    def _navigate_to_workspace(project: str | None, rendered_route: str | None):
        target = f"{ROUTES_BASE}/project/{project}" if project else f"{ROUTES_BASE}/"
        # The picker's value is mirrored from project-store on load and
        # hydration; a target the rendered route already shows would only
        # re-fire every pathname-driven callback for a second lap.
        if target == rendered_route:
            raise PreventUpdate
        # On the artifact viewer the picker mirrors the artifact's
        # project; that adoption must not hijack the viewer route. The
        # investigation pages mirror their project the same way.
        rendered = parse_route(rendered_route)
        # The sweep and exceptions pages mirror their project the same
        # way (rewrite epic jernerics-xjxa): a mirrored picker value
        # must not hijack their routes either.
        if rendered.kind in (
            "artifact",
            "investigation",
            "investigation-edit",
            "sweep",
            "exceptions",
        ):
            raise PreventUpdate
        # A genuine project switch starts a fresh scope: the previous
        # project's ?view= must not ride along, or its hydration would
        # pin the old sweeps onto the new project.
        if rendered.kind == "workspace" and rendered.object_id != project:
            return target, ""
        return target, no_update

    @app.callback(
        Output("project-picker", "value"),
        Input("project-store", "data"),
        Input("url", "search"),
        State("project-picker", "value"),
    )
    def _show_current_project(
        project: str | None, search: str | None, picked: str | None
    ):
        # The picker mirrors project-store; with nothing picked, a
        # shared ?sel= token names the project for a fresh session
        # (jernerics-xbx). Picking it here — a plain write through the
        # picker's own callback — runs the exact settle path of a
        # manual pick, so the label follows and a chosen project is
        # never overridden. A view-only URL edit must not re-run that
        # settle path and cascade through the project store.
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if project:
            if "project-store.data" in triggered:
                # An identical mirror write re-fires the picker's own
                # watchers and re-enters the store; skip it.
                if project == picked:
                    raise PreventUpdate
                return project
            raise PreventUpdate
        selection, _error = analysis.cold_start(service, search)
        return selection.project if selection else None

    @app.callback(
        Output("project-store", "data"),
        Input("project-picker", "value"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _remember_project(project: str | None, current: str | None):
        if project == current:
            raise PreventUpdate
        return project

    @app.callback(
        Output("project-store", "data", allow_duplicate=True),
        Input("url", "pathname"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _adopt_project_from_url(pathname: str | None, current: str | None):
        spec = parse_route(pathname)
        if spec.kind in (
            "workspace",
            "investigation",
            "investigation-edit",
            "sweep",
        ):
            if spec.object_id == current:
                raise PreventUpdate
            return spec.object_id
        raise PreventUpdate

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("sweep-grid", "selectedRows"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _select_sweeps(rows: list[dict] | None, current: dict | None):
        doc = analysis.edited_view(
            current,
            {"scope": tray_from_grid(rows, (current or {}).get("scope"))},
        )
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("selection-tray", "children"),
        Output("selection-tray", "style"),
        Input("view-store", "data"),
        Input("project-store", "data"),
    )
    def _update_tray(view_doc: dict | None, _project: str | None):
        # The header summary is the one-click door into the scope
        # browser — it opens the browser, never a separate page.
        summary = analysis.tray_summary((view_doc or {}).get("scope"))
        if not summary:
            return "", {"display": "none"}
        return summary, {}

    @app.callback(
        Output("scope-browser", "open"),
        Input("selection-tray", "n_clicks"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _open_scope_browser(_clicks: int | None, pathname: str | None):
        # The tray lives in the shell; the browser only exists on the
        # workspace page, so off-workspace clicks must not dispatch.
        if parse_route(pathname).kind != "workspace":
            raise PreventUpdate
        return True

    # -- Scope browser and workspace curation ----------------------------

    @app.callback(
        Output("workspace-store", "data"),
        Input("workspace-quick", "value"),
        Input("sweep-grid", "filterModel"),
        Input("sweep-grid", "columnState"),
        Input({"overview-grid": dash.ALL}, "columnState"),
        State("project-store", "data"),
        State("workspace-store", "data"),
        prevent_initial_call=True,
    )
    def _remember_workspace(
        quick: str | None,
        filters: dict | None,
        columns: list | None,
        overview_columns: list | None,
        project: str | None,
        current: dict | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        fields: dict[str, object] = {}
        if "workspace-quick.value" in triggered:
            fields["quick"] = quick or ""
        if "sweep-grid.filterModel" in triggered:
            fields["filters"] = filters or None
        if "sweep-grid.columnState" in triggered:
            fields["sort"] = sort_from_columns(columns)
        if any('"overview-grid"' in text for text in triggered):
            overview_state = next(
                (state for state in overview_columns or [] if state is not None),
                None,
            )
            fields["overview_sort"] = sort_from_columns(overview_state)
        updated = remember_workspace(current, project, **fields)
        if updated is None:
            raise PreventUpdate
        return updated

    @app.callback(
        Output("sweep-grid", "rowData"),
        Output("sweep-grid", "selectedRows"),
        Output("workspace-curation-note", "children"),
        Output("sweep-browser-facts-store", "data"),
        Input("project-store", "data"),
        Input("view-store", "data"),
        Input("poll", "n_intervals"),
        State("sweep-grid", "selectedRows"),
        State("sweep-browser-facts-store", "data"),
    )
    def _load_browser_sweeps(
        project: str | None,
        view_doc: dict | None,
        _tick: int | None,
        grid_selection: list[dict] | None,
        facts: dict | None,
    ):
        # Scope data runs only for project/scope/refresh changes; every
        # other view-store write re-renders nothing here.
        doc = view_doc or analysis.default_view_state()
        scope = doc.get("scope") or analysis.default_scope_state()
        desired = {
            "project": project,
            "sweeps": sorted(str(s) for s in scope.get("sweeps") or []),
            "include_archived": bool(scope.get("include_archived")),
            "include_invalid": bool(scope.get("include_invalid")),
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        stored = {k: v for k, v in (facts or {}).items() if k != "digest"}
        if "view-store.data" in triggered and desired == stored:
            raise PreventUpdate
        if not project:
            return [], analysis.mounted_selection([], initial=is_initial()), "", desired
        rows = workspace.browser_sweep_rows(
            service.sweep_overview(project),
            scope,
            include_archived=desired["include_archived"],
            include_invalid=desired["include_invalid"],
        )
        # The grid's live selection joins the tray: a poll tick can
        # dispatch before a just-made selection lands in the view doc,
        # and re-deriving from the tray alone would clear it.
        picked = set(desired["sweeps"]) | {
            str(row["sweep_id"]) for row in grid_selection or []
        }
        selected = analysis.mounted_selection(
            [row for row in rows if row["sweep_id"] in picked],
            initial=is_initial(),
        )
        note = workspace.curation_note(rows)
        digest = _content_digest(rows, selected, note)
        if digest == (facts or {}).get("digest"):
            raise PreventUpdate
        return rows, selected, note, {**desired, "digest": digest}

    @app.callback(
        Output("analysis-family-grid", "columnDefs"),
        Output("analysis-family-grid", "rowData"),
        Output("analysis-family-grid", "selectedRows"),
        Output("trial-browser-facts-store", "data"),
        Input("view-store", "data"),
        Input("project-store", "data"),
        Input("poll", "n_intervals"),
        State("trial-browser-facts-store", "data"),
    )
    def _load_browser_families(
        view_doc: dict | None,
        project: str | None,
        _tick: int | None,
        facts: dict | None,
    ):
        # The trial browser consumes the scope, the color choice, and
        # the series payload; any other view edit leaves it untouched.
        doc = view_doc or analysis.default_view_state()
        desired = {
            "scope": analysis.scope_dims(doc.get("scope")),
            "color": doc["series"]["color"],
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        stored = {k: v for k, v in (facts or {}).items() if k != "digest"}
        if "view-store.data" in triggered and desired == stored:
            raise PreventUpdate
        columns, rows, selected = analysis.browser_trial_outputs(
            service, project, doc.get("scope"), view_doc
        )
        selected = analysis.mounted_selection(selected, initial=is_initial())
        digest = _content_digest(columns, rows, selected)
        if digest == (facts or {}).get("digest"):
            raise PreventUpdate
        return columns, rows, selected, {**desired, "digest": digest}

    @app.callback(
        Output("sweep-grid", "dashGridOptions"),
        Input("workspace-quick", "value"),
        prevent_initial_call=True,
    )
    def _filter_sweep_rows(text: str | None):
        return components.grid_options(
            rowSelection={"mode": "multiRow"}, quickFilterText=text or ""
        )

    @app.callback(
        Output("ws-archive", "disabled"),
        Output("ws-invalid", "disabled"),
        Output("ws-restore-validity", "disabled"),
        Output("ws-restore", "disabled"),
        Output("ws-reason", "style"),
        Output("ws-curation-summary", "children"),
        Input("sweep-grid", "selectedRows"),
        prevent_initial_call=True,
    )
    def _offer_workspace_transitions(rows: list[dict] | None):
        offered = workspace.selection_transitions(rows)
        return (
            not offered["archive"],
            not offered["invalid"],
            not offered["restore_validity"],
            not offered["restore"],
            # The reason input exists only while Mark invalid is offered;
            # otherwise it would sit permanently visible and empty.
            {} if offered["invalid"] else {"display": "none"},
            workspace.curation_summary(len(rows or [])),
        )

    WORKSPACE_ACTIONS = {
        "ws-archive.n_clicks": "archive",
        "ws-invalid.n_clicks": "invalid",
        "ws-restore-validity.n_clicks": "restore_validity",
        "ws-restore.n_clicks": "restore",
    }

    @app.callback(
        Output("workspace-message", "children"),
        Output("sweep-grid", "rowData", allow_duplicate=True),
        Output("sweep-grid", "selectedRows"),
        Output("workspace-curation-note", "children", allow_duplicate=True),
        Input("ws-archive", "n_clicks"),
        Input("ws-invalid", "n_clicks"),
        Input("ws-restore-validity", "n_clicks"),
        Input("ws-restore", "n_clicks"),
        State("sweep-grid", "selectedRows"),
        State("ws-reason", "value"),
        State("project-store", "data"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _curate_from_workspace(
        _archive: int,
        _invalid: int,
        _validity: int,
        _restore: int,
        rows: list[dict] | None,
        reason: str | None,
        project: str | None,
        view_doc: dict | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        action = triggered_action(triggered, WORKSPACE_ACTIONS)
        if action is None:
            raise PreventUpdate
        sweep_ids = [str(row["sweep_id"]) for row in rows or []]
        if not sweep_ids:
            return (
                workspace.action_message(
                    False, "Select sweeps first — actions apply to selected rows."
                ),
                no_update,
                no_update,
                no_update,
            )
        ok, report = apply_curation(service, action, sweep_ids, reason or "")
        fresh, note = _post_action_grid(service, project, view_doc)
        # Rows recompute from the CURRENT tray, so picked curated sweeps
        # never flicker out; the selection keeps every row that survived
        # the action — rows that legitimately left discovery are gone.
        # Writing the survivors (or []) keeps the write an event, so the
        # tray and action-bar callbacks re-fire against the new state.
        kept = {str(row["sweep_id"]) for row in rows or []}
        return (
            workspace.action_message(ok, report),
            fresh,
            [row for row in fresh if row["sweep_id"] in kept],
            note,
        )

    # -- Failure view: the roll-up's scope-wide failed executions --------

    @app.callback(
        Output("failed-trials-panel", "children"),
        Output("sweep-grid", "rowData", allow_duplicate=True),
        Output("sweep-grid", "selectedRows", allow_duplicate=True),
        Output("workspace-curation-note", "children", allow_duplicate=True),
        Output({"failed-sweep": dash.ALL}, "value"),
        Input({"failed-invalid": dash.ALL}, "n_clicks"),
        Input("failed-invalid-batch", "n_clicks"),
        Input({"failed-sweep": dash.ALL}, "value"),
        Input("failed-select-all", "value"),
        State("failed-reason", "value"),
        State("project-store", "data"),
        State("view-store", "data"),
        State("sweep-grid", "selectedRows"),
        prevent_initial_call=True,
    )
    def _drive_failed_view(
        _invalid: list,
        _batch: int | None,
        checks: list,
        select_all: list,
        reason: str | None,
        project: str | None,
        view_doc: dict | None,
        grid_selection: list[dict] | None,
    ):
        # One entry point: a group's Mark invalid or the batch button
        # acts, then re-renders the view; select-all mirrors onto the
        # group checklists. The Exceptions tab mounts the view open and
        # already filled.
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        value, name = pattern_trigger(dash.callback_context)
        scoped = workspace.scoped_sweeps(
            service.sweep_overview(project or ""), (view_doc or {}).get("scope")
        )
        # ALL-input writes and re-render remounts re-fire this callback —
        # only an explicit control acts, everything else stays put.
        untouched = [no_update] * len(checks or [])
        if "failed-select-all.value" in triggered:
            checked = bool(select_all)
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                [
                    [sweep_id] if checked else []
                    for sweep_id in mounted_failed_sweep_ids()
                ],
            )
        if name == "failed-invalid" and value:
            ok, report = apply_curation(service, "invalid", [str(value)], reason or "")
            # The action refreshes the grid too — both surfaces move
            # together, with the surviving selection and the note.
            fresh, note = _post_action_grid(service, project, view_doc)
            kept = {str(row["sweep_id"]) for row in grid_selection or []}
            return (
                workspace.failed_view_panel(
                    service,
                    project or "",
                    scoped,
                    time.time_ns(),
                    workspace.action_message(ok, report),
                ),
                fresh,
                [row for row in fresh if row["sweep_id"] in kept],
                note,
                untouched,
            )
        if "failed-invalid-batch.n_clicks" in triggered:
            sweep_ids = selected_failed_sweeps(checks)
            if not sweep_ids:
                return (
                    workspace.failed_view_panel(
                        service,
                        project or "",
                        scoped,
                        time.time_ns(),
                        workspace.action_message(
                            False,
                            "Select sweeps first — "
                            "actions apply to checked failed sweeps.",
                        ),
                    ),
                    no_update,
                    no_update,
                    no_update,
                    untouched,
                )
            ok, report = apply_curation(service, "invalid", sweep_ids, reason or "")
            fresh, note = _post_action_grid(service, project, view_doc)
            kept = {str(row["sweep_id"]) for row in grid_selection or []}
            return (
                workspace.failed_view_panel(
                    service,
                    project or "",
                    scoped,
                    time.time_ns(),
                    workspace.action_message(ok, report),
                ),
                fresh,
                [row for row in fresh if row["sweep_id"] in kept],
                note,
                untouched,
            )
        raise PreventUpdate

    # -- Sweep page: live region, curation, retry-root picking ------------

    @app.callback(
        Output("sweep-page-body", "children"),
        Output("sweep-page-facts-store", "data"),
        Input("poll", "n_intervals"),
        State("url", "pathname"),
        State("url", "search"),
        State("view-store", "data"),
        State("sweep-page-facts-store", "data"),
        prevent_initial_call=True,
    )
    def _refresh_sweep_page(
        _tick: int | None,
        pathname: str | None,
        search: str | None,
        view_doc: dict | None,
        facts: dict | None,
    ):
        spec = parse_route(pathname)
        if spec.kind != "sweep":
            raise PreventUpdate
        sweep_id = spec.sub_id or ""
        # Facts before trees: the cheap overview gate skips the full
        # sweep read on every unchanged tick.
        cheap = _content_digest(service.sweep_facts(sweep_id))
        if cheap == (facts or {}).get("cheap"):
            raise PreventUpdate
        data = sweep.collect(service, sweep_id, sweep.via_from_search(search))
        if data is None:
            raise PreventUpdate
        fresh = sweep.digest(data)
        if fresh == (facts or {}).get("digest"):
            return no_update, {"digest": fresh, "cheap": cheap}
        picked = set(((view_doc or {}).get("scope") or {}).get("families") or ())
        body = sweep.render(
            data, spec.object_id or "", sweep_id, time.time_ns(), picked
        )
        return body, {"digest": fresh, "cheap": cheap}

    SWEEP_ACTIONS = {
        "sweep-archive.n_clicks": "archive",
        "sweep-invalid.n_clicks": "invalid",
        "sweep-restore-validity.n_clicks": "restore_validity",
        "sweep-restore.n_clicks": "restore",
    }

    @app.callback(
        Output("sweep-message", "children"),
        Output("sweep-page-body", "children", allow_duplicate=True),
        Output("sweep-page-facts-store", "data", allow_duplicate=True),
        Input("sweep-archive", "n_clicks"),
        Input("sweep-invalid", "n_clicks"),
        Input("sweep-restore-validity", "n_clicks"),
        Input("sweep-restore", "n_clicks"),
        State("sweep-reason", "value"),
        State("url", "pathname"),
        State("url", "search"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _curate_from_sweep_page(
        _archive: int,
        _invalid: int,
        _validity: int,
        _restore: int,
        reason: str | None,
        pathname: str | None,
        search: str | None,
        view_doc: dict | None,
    ):
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        action = triggered_action(triggered, SWEEP_ACTIONS)
        spec = parse_route(pathname)
        if action is None or spec.kind != "sweep":
            raise PreventUpdate
        sweep_id = spec.sub_id or ""
        ok, report = apply_curation(service, action, [sweep_id], reason or "")
        data = sweep.collect(service, sweep_id, sweep.via_from_search(search))
        if data is None:
            return workspace.action_message(ok, report), no_update, no_update
        picked = set(((view_doc or {}).get("scope") or {}).get("families") or ())
        body = sweep.render(
            data, spec.object_id or "", sweep_id, time.time_ns(), picked
        )
        return (
            workspace.action_message(ok, report),
            body,
            {
                "digest": sweep.digest(data),
                "cheap": _content_digest(service.sweep_facts(sweep_id)),
            },
        )

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"sweep-trial-pick": dash.ALL}, "value"),
        State("view-store", "data"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _pick_retry_roots(
        values: list,
        current: dict | None,
        pathname: str | None,
    ):
        # Trial checkboxes on the sweep page are the retry-root picker's
        # permanent home: checking merges this sweep's roots into the
        # scope's families; unchecking drops exactly this sweep's roots.
        spec = parse_route(pathname)
        if spec.kind != "sweep" or not spec.object_id:
            raise PreventUpdate
        checked = {str(entry[0]) for entry in values or [] if entry}
        roots = {
            str(row["root"])
            for row in service.analysis_families(spec.object_id, [spec.sub_id or ""])
        }
        scope = dict(
            (current or analysis.default_view_state()).get("scope")
            or analysis.default_scope_state()
        )
        merged = sorted((set(scope.get("families") or []) - roots) | checked)
        if merged == sorted(str(v) for v in scope.get("families") or []):
            raise PreventUpdate
        scope["families"] = merged
        # A pattern-input callback returns its single output as a tuple.
        return (analysis.edited_view(current, {"scope": scope}),)

    app.clientside_callback(
        """
        function(clicks) {
            return (clicks || []).map((c) => !(c && c % 2 === 1));
        }
        """,
        Output({"trial-subrow": dash.ALL}, "hidden"),
        Input({"trial-toggle": dash.ALL}, "n_clicks"),
    )
    app.clientside_callback(
        """
        function(clicks) {
            return (clicks || []).map((c) => (c && c % 2 === 1) ? "▾" : "▸");
        }
        """,
        Output({"trial-chev": dash.ALL}, "children"),
        Input({"trial-toggle": dash.ALL}, "n_clicks"),
    )

    @app.callback(
        Output("overview-digest-store", "data"),
        Input("view-store", "data"),
        Input("poll", "n_intervals"),
        Input({"analysis-tabs": ALL}, "value"),
        State("project-store", "data"),
        State("overview-digest-store", "data"),
    )
    def _track_overview_digest(
        view_doc: dict | None,
        _tick: int | None,
        tabs: list | None,
        project: str | None,
        digest_doc: dict | None,
    ):
        # Shell-only twin of the overview renderer: jernerics-8c9 bans
        # mixing shell and page outputs on one shell-firable callback,
        # so the digest lives here and the region renders separately.
        _facts, digest = overview_content(
            service,
            project,
            (view_doc or {}).get("scope"),
            (view_doc or {}).get("overview_filter"),
        )
        tab = (tabs or [None])[0]
        if digest == (digest_doc or {}).get("digest"):
            raise PreventUpdate
        return {"digest": digest}

    @app.callback(
        Output("workspace-overview", "children"),
        Input("view-store", "data"),
        Input("poll", "n_intervals"),
        Input({"analysis-tabs": ALL}, "value"),
        State("project-store", "data"),
        State("overview-digest-store", "data"),
        State("workspace-store", "data"),
    )
    def _render_overview(
        view_doc: dict | None,
        _tick: int | None,
        tabs: list | None,
        project: str | None,
        digest_doc: dict | None,
        workspace_doc: dict | None,
    ):
        doc = view_doc or analysis.default_view_state()
        tab = (tabs or [None])[0]
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        _facts, digest = overview_content(
            service,
            project,
            (view_doc or {}).get("scope"),
            doc.get("overview_filter"),
        )
        # A view edit must always re-render: the digest mirror may have
        # already advanced to this dispatch's digest, and the gate is
        # only here to throttle poll ticks (jernerics-haj).
        if "view-store.data" not in triggered and digest == (digest_doc or {}).get(
            "digest"
        ):
            raise PreventUpdate
        sort = workspace_state(workspace_doc, project).get("overview_sort")
        return workspace.overview_tab(
            service,
            project,
            (view_doc or {}).get("scope"),
            overview_filter=doc.get("overview_filter"),
            sort=sort,
        )

    # -- Overview tiles, scope seg, and Create Investigation --------------

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"overview-tile": dash.ALL}, "n_clicks"),
        Input({"overview-filter-clear": dash.ALL}, "n_clicks"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_overview_filter(_tiles: list, _clear: list, current: dict | None):
        # A tile click sets its filter; clicking the active tile (or the
        # chip's ×) clears it — every tile state has a one-click way back.
        # Remounts re-fire the pattern inputs with click counts of None;
        # only a real press acts.
        if not pressed_props(dash.callback_context):
            raise PreventUpdate
        value, control = pattern_trigger(dash.callback_context)
        if control == "overview-filter-clear":
            doc = analysis.edited_view(current, {"overview_filter": None})
            if doc == (current or {}):
                raise PreventUpdate
            return doc
        if control != "overview-tile" or value is None:
            raise PreventUpdate
        active = (current or {}).get("overview_filter")
        doc = analysis.edited_view(
            current, {"overview_filter": None if active == value else str(value)}
        )
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("overview-scope-active", "n_clicks"),
        Input("overview-scope-all", "n_clicks"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_overview_scope(
        _active: int | None, _all: int | None, current: dict | None
    ):
        # The seg control drives the same include flags as the Browse
        # toggles: Active is default discovery, All is every sweep.
        # Overview re-renders remount the buttons, re-firing this with
        # click counts of None — only a real press acts.
        pressed = pressed_props(dash.callback_context)
        if not pressed:
            raise PreventUpdate
        values = (
            ["archived", "invalid"] if "overview-scope-all.n_clicks" in pressed else []
        )
        doc = analysis.view_from_include(current, values)
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("overview-selection-count", "children"),
        Output("overview-create-investigation", "disabled"),
        Output("overview-bulkbar", "style"),
        Input({"overview-grid": dash.ALL}, "selectedRows"),
        prevent_initial_call=True,
    )
    def _offer_overview_actions(rows: list):
        picked_rows = next(
            (entry for entry in reversed(rows or []) if entry is not None), None
        )
        if picked_rows is None:
            raise PreventUpdate
        picked = len(picked_rows)
        return (
            f"{workspace.counted_sweeps(picked)} selected" if picked else "",
            picked == 0,
            {} if picked else {"display": "none"},
        )

    @app.callback(
        Output({"overview-grid": dash.ALL}, "selectedRows", allow_duplicate=True),
        Input("overview-clear-selection", "n_clicks"),
        prevent_initial_call=True,
    )
    def _clear_overview_selection(_clicks: int | None):
        return []

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input("overview-create-investigation", "n_clicks"),
        State({"overview-grid": dash.ALL}, "selectedRows"),
        State("project-store", "data"),
        prevent_initial_call=True,
    )
    def _create_investigation_from_selection(
        _clicks: int | None,
        rows: list,
        project: str | None,
    ):
        # The editor route (jernerics-g5rw.8) reads the ?sweeps= seed.
        picked_rows = next(
            (entry for entry in reversed(rows or []) if entry is not None), []
        )
        if not project or not picked_rows:
            raise PreventUpdate
        return investigation_new_href(
            project, [str(row["sweep_id"]) for row in picked_rows]
        )

    # -- Investigations and Exceptions tabs ------------------------------

    @app.callback(
        Output("workspace-investigations", "children"),
        Input("view-store", "data"),
        Input({"analysis-tabs": ALL}, "value"),
        State("project-store", "data"),
    )
    def _render_investigations(
        view_doc: dict | None, tabs: list | None, project: str | None
    ):
        tab = (tabs or [None])[0]
        if tab != "investigations":
            raise PreventUpdate
        return workspace.investigations_tab(service, project)

    @app.callback(
        Output("workspace-exceptions", "children"),
        Input("view-store", "data"),
        Input({"analysis-tabs": ALL}, "value"),
        State("project-store", "data"),
    )
    def _render_exceptions(
        view_doc: dict | None, tabs: list | None, project: str | None
    ):
        tab = (tabs or [None])[0]
        if tab != "exceptions":
            raise PreventUpdate
        return workspace.exceptions_tab(
            service, project, (view_doc or {}).get("scope"), time.time_ns()
        )

    # -- Investigation workspace and member editor (jernerics-g5rw.8) ----

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input("compare-members-grid", "cellClicked"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _open_member_sweep(click: dict | None, pathname: str | None):
        # A row click on the member inventory opens the sweep page with
        # the ``?via=`` return path to this investigation.
        spec = parse_route(pathname)
        if spec.kind != "investigation" or not isinstance(click, dict):
            raise PreventUpdate
        row_id = click.get("rowId")
        if not row_id:
            raise PreventUpdate
        return (
            f"{ROUTES_BASE}/project/{spec.object_id}/sweep/{row_id}",
            f"?via={spec.sub_id or ''}",
        )

    @app.callback(
        Output({"inv-compare": ALL}, "children"),
        Input({"inv-compare-toggle": ALL}, "value"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _render_compare(values: list | None, pathname: str | None):
        spec = parse_route(pathname)
        if spec.kind != "investigation" or not spec.sub_id:
            raise PreventUpdate
        include_invalid = bool(values and values[0])
        detail = service.investigation_detail(spec.sub_id)
        doc = service.investigation_compare(
            spec.sub_id, include_invalid=include_invalid
        )
        return [
            workspace.compare_children(
                doc,
                spec.object_id or "",
                detail.investigation.outcome,
                include_invalid,
            )
        ]

    # -- Investigation views, member scope, and analysis regions ----------

    def _inv_context(
        pathname: str | None, view_doc: dict | None
    ) -> tuple[Any, dict[str, Any] | None]:
        """(investigation record, resolved scope group) for the page a
        URL names; the member-scope resolution folds an unknown member
        back to the full membership."""
        spec = parse_route(pathname)
        if spec.kind != "investigation" or not spec.sub_id:
            return None, None
        try:
            record = service.investigation_detail(spec.sub_id).investigation
        except CurationRejectedError:
            return None, None
        doc = view_doc or analysis.default_view_state()
        tray, _scoped = analysis.investigation_scope_state(
            record.members, (doc.get("inv") or {}).get("member")
        )
        return record, tray

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"inv-view": ALL}, "n_clicks"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_inv_view(_clicks: list | None, current: dict | None):
        # The edit carries the switch into the view document so the URL
        # follows. Compare visibly drops a member scope: a one-member
        # scope must never pose as the full comparison.
        if not pressed_props(dash.callback_context):
            raise PreventUpdate
        view, control = pattern_trigger(dash.callback_context)
        if control != "inv-view" or view not in analysis.INVESTIGATION_VIEWS:
            raise PreventUpdate
        doc = analysis.view_from_inv(current, view=str(view))
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output({"inv-view": ALL}, "className"),
        Output({"inv-region": ALL}, "style"),
        Input("view-store", "data"),
    )
    def _sync_inv_regions(view_doc: dict | None):
        # Button marks and region visibility follow the view document
        # alone, in INVESTIGATION_VIEWS order — the order the DOM mounts
        # them. The outputs stay one pattern family: mixing the shell's
        # store write into the same dispatch is what corrupted it.
        active = (view_doc or {}).get("inv", {}).get("view") or "compare"
        return (
            ["on" if name == active else "" for name in analysis.INVESTIGATION_VIEWS],
            [
                {"display": "block" if name == active else "none"}
                for name in analysis.INVESTIGATION_VIEWS
            ],
        )

    app.clientside_callback(
        """
        function(styles) {
            // A region shown after loading hidden renders its plotly
            // figures at a stale size; a resize event makes dcc.Graph
            // re-measure.
            window.dispatchEvent(new Event("resize"));
            return window.dash_clientside.no_update;
        }
        """,
        Output("inv-points-echo", "data"),
        Input({"inv-region": ALL}, "style"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"inv-member-clear": ALL}, "n_clicks"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _clear_inv_member(_clicks: int | None, current: dict | None):
        if not pressed_props(dash.callback_context):
            raise PreventUpdate
        doc = analysis.view_from_inv(current, member="")
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output({"inv-member-note": ALL}, "children"),
        Output({"inv-member-clear": ALL}, "style"),
        Output({"inv-crumb-member": ALL}, "children"),
        Output({"inv-python": ALL}, "children"),
        Input("view-store", "data"),
        State("url", "pathname"),
    )
    def _sync_member_scope(view_doc: dict | None, pathname: str | None):
        # The member-scope surfaces ride one store write; the shell's
        # python panel content and the crumb live region update without
        # remounting the page. Off the investigation route nothing here
        # is mounted, so the pattern outputs are the only safe writers.
        spec = parse_route(pathname)
        if spec.kind != "investigation" or not spec.sub_id:
            raise PreventUpdate
        try:
            record = service.investigation_detail(spec.sub_id).investigation
        except CurationRejectedError:
            raise PreventUpdate from None
        doc = view_doc or analysis.default_view_state()
        _tray, scoped = analysis.investigation_scope_state(
            record.members, (doc.get("inv") or {}).get("member")
        )
        label = None
        if scoped:
            focused = service.sweep_detail(scoped)
            label = focused.overview.name if focused else short_id(scoped)
        crumb = [html.Span(className="dim"), html.Span(label)] if label else []
        return (
            [f"Scoped to member {label}" if label else ""],
            [{} if label else {"display": "none"}],
            [crumb],
            [workspace.python_panel(record, scoped)],
        )

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-key", "value"),
        Input("analysis-mode", "value"),
        Input("analysis-reduction", "value"),
        Input("analysis-display", "value"),
        Input("analysis-auto-refresh", "value"),
        Input("analysis-color", "value"),
        Input("analysis-facet", "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_inv_view_state(
        keys: list | None,
        mode: str | None,
        reduction: str | None,
        display: str | None,
        auto_flags: list | None,
        color: str | None,
        facet: str | None,
        current: dict | None,
    ):
        # The investigation Series controls have no analysis-tabs/contour
        # siblings, so the workspace control-edit callback never wires
        # here (a missing input unwires the whole callback); this variant
        # carries the same edits without them.
        edited = analysis.edited_fields(dash.callback_context.triggered_prop_ids)
        if not edited or len(edited) > 2:
            raise PreventUpdate
        doc = analysis.view_from_controls(
            current,
            active=None,
            keys=keys,
            mode=mode,
            reduction=reduction,
            color=color,
            facet=facet,
            contour_x=None,
            contour_y=None,
            trial_display=display,
            auto_refresh="auto" in (auto_flags or []),
            edited=edited,
        )
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("analysis-key", "value"),
        Output("analysis-mode", "value"),
        Output("analysis-reduction", "value"),
        Output("analysis-color", "value"),
        Output("analysis-facet", "value"),
        Output("analysis-display", "value"),
        Output("analysis-auto-refresh", "value"),
        Input("view-store", "data"),
        Input("analysis-key", "options"),
        Input("analysis-color", "options"),
        Input("analysis-facet", "options"),
    )
    def _sync_inv_controls(
        doc: dict | None,
        key_options: list | None,
        color_options: list | None,
        facet_options: list | None,
    ):
        # Control values ride with their options so a value written
        # before its options exist is not dropped and echoed back as a
        # spurious clear. Dropdown values arrive only once their options
        # carry them (analysis.control_values gates each one).
        values = analysis.control_values(
            doc,
            {
                "keys": analysis.loaded_option_values(key_options),
                "color": analysis.loaded_option_values(color_options),
                "facet": analysis.loaded_option_values(facet_options),
            },
        )
        return (
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
            values[8],
            values[9],
        )

    @app.callback(
        Output("analysis-series-data", "data"),
        Output("analysis-updated", "children"),
        Output({"analysis-refresh-store": ALL}, "data"),
        Input("view-store", "data"),
        Input({"analysis-refresh": ALL}, "n_clicks"),
        Input("poll", "n_intervals"),
        State("url", "pathname"),
        State("analysis-series-data", "data"),
        prevent_initial_call=True,
    )
    def _load_inv_series_snapshot(
        view_doc: dict | None,
        _clicks: int | None,
        _tick: int | None,
        pathname: str | None,
        snapshot: dict | None,
    ):
        # Only the investigation's scope, a manual refresh, an enabled
        # poll, or a series-view activation fetch series data; a usable
        # snapshot renders from the store instead. Outputs stay
        # page-local: jernerics-8c9 forbids mixing the shell's view
        # store into a callback the shell can fire, so the
        # auto-refresh flip lives in the router callback instead.
        doc = view_doc or analysis.default_view_state()
        if (doc.get("inv") or {}).get("view") != "series":
            raise PreventUpdate
        record, tray = _inv_context(pathname, view_doc)
        if record is None:
            raise PreventUpdate
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if "view-store.data" in triggered and "poll.n_intervals" not in triggered:
            usable, _missing = analysis.snapshot_status(
                snapshot,
                analysis.scope_fingerprint(record.project, tray),
                doc["series"]["reduction"],
                doc["series"]["keys"],
            )
            if usable:
                raise PreventUpdate
        now = time.time_ns()
        try:
            data, updated, state = analysis.series_data_outputs(
                service, record.project, tray, doc, now
            )
        except Exception as error:
            data, updated, state = analysis.series_data_failure(error, now)
        # The pattern-ALL store output takes one entry per mounted store.
        return data, updated, [state]

    @app.callback(
        Output("analysis-series-panels", "children"),
        Output("analysis-series-data", "data", allow_duplicate=True),
        Output("analysis-key", "options"),
        Output("analysis-color", "options"),
        Output("analysis-facet", "options"),
        Output("analysis-context-filters", "children"),
        Output("analysis-series-status", "children"),
        Output("analysis-series-figure-store", "data"),
        Output("analysis-series-figure", "figure"),
        Input("view-store", "data"),
        Input("analysis-series-data", "data"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _render_inv_series(
        view_doc: dict | None,
        snapshot: dict | None,
        pathname: str | None,
    ):
        # Presentation rebuilds from the stored snapshot: view-only
        # edits issue zero reads, added keys fetch only the missing
        # ones, scope/reduction changes rebuild.
        doc = view_doc or analysis.default_view_state()
        if (doc.get("inv") or {}).get("view") != "series":
            raise PreventUpdate
        record, tray = _inv_context(pathname, view_doc)
        if record is None:
            raise PreventUpdate
        if snapshot is None:
            raise PreventUpdate
        try:
            (
                panels,
                persist,
                key_options,
                color_options,
                facet_options,
                filters,
                status,
                figure,
            ) = analysis.series_view_outputs(
                service,
                record.project,
                tray,
                doc,
                snapshot,
                time.time_ns(),
            )
        except Exception as error:
            failure = analysis.series_view_failure(error)
            return (*failure[:7], no_update, failure[7])
        return (
            panels,
            persist,
            key_options,
            color_options,
            facet_options,
            filters,
            status,
            _figure_payload(figure),
            figure,
        )

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Output({"axis-note": dash.ALL}, "children"),
        Input({"axis-scale": dash.ALL}, "value"),
        Input({"axis-range": dash.ALL}, "value"),
        Input({"axis-min": dash.ALL}, "value"),
        Input({"axis-max": dash.ALL}, "value"),
        Input({"axis-reset": dash.ALL}, "n_clicks"),
        State("view-store", "data"),
        State("analysis-series-data", "data"),
        prevent_initial_call=True,
    )
    def _edit_inv_axis_state(
        _scales: list,
        _ranges: list,
        _lows: list,
        _highs: list,
        _resets: list,
        current: dict | None,
        data: dict | None,
    ):
        # ALL (not MATCH): dash's dropdown children extend the pattern
        # id and break single-value MATCH resolution; values are read
        # from inputs_list by resolved id.
        inputs = dash.callback_context.inputs_list
        metric, control = pattern_trigger(dash.callback_context)
        field = control.removeprefix("axis-") if control else ""
        if not metric or field not in {"scale", "range", "min", "max", "reset"}:
            raise PreventUpdate
        doc, note = analysis.axis_state_edit(
            current,
            metric=metric,
            control=field,
            scale=pattern_input_value(inputs, 0, "axis-scale", metric),
            range_mode=pattern_input_value(inputs, 1, "axis-range", metric),
            low=pattern_input_value(inputs, 2, "axis-min", metric),
            high=pattern_input_value(inputs, 3, "axis-max", metric),
            data=data,
        )
        if doc is None and note is None:
            raise PreventUpdate
        notes = analysis.panel_notes(current, data)
        if (current or analysis.default_view_state())["series"]["mode"] == "stacked":
            keys = (current or analysis.default_view_state())["series"]["keys"]
            if metric in keys:
                notes[keys.index(metric)] = note or notes[keys.index(metric)]
        return no_update if doc is None else doc, notes

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Output("analysis-overlay-note", "children"),
        Input("analysis-overlay-scale", "value"),
        Input("analysis-overlay-range", "value"),
        Input("analysis-overlay-min", "value"),
        Input("analysis-overlay-max", "value"),
        Input("analysis-overlay-reset", "n_clicks"),
        State("view-store", "data"),
        State("analysis-series-data", "data"),
        prevent_initial_call=True,
    )
    def _edit_inv_overlay_axis(
        scale: str | None,
        range_mode: str | None,
        low: Any,
        high: Any,
        _reset: int | None,
        current: dict | None,
        data: dict | None,
    ):
        control = overlay_axis_control(dash.callback_context.triggered)
        if control is None:
            raise PreventUpdate
        doc, note = analysis.axis_state_edit(
            current,
            metric=None,
            control=control,
            scale=scale,
            range_mode=range_mode,
            low=low,
            high=high,
            data=data,
        )
        if doc is None and note is None:
            raise PreventUpdate
        return no_update if doc is None else doc, note

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"panel-move-up": dash.ALL}, "n_clicks"),
        Input({"panel-move-down": dash.ALL}, "n_clicks"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _move_inv_series_key(_ups: list, _downs: list, current: dict | None):
        metric, control = pattern_trigger(dash.callback_context)
        if metric is None or control not in ("panel-move-up", "panel-move-down"):
            raise PreventUpdate
        doc = analysis.moved_keys(current, metric, control.removeprefix("panel-move-"))
        if doc is None:
            raise PreventUpdate
        return doc

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-series-figure", "clickData"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _focus_inv_trace(click: dict | None, current: dict | None):
        # A trace click highlights and focuses that trial — focus is
        # highlight-only here; nothing mutates membership.
        doc = analysis.view_from_trace_click(current, click)
        if doc is None or doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output({"analysis-error": ALL}, "children", allow_duplicate=True),
        Input({"analysis-refresh-store": ALL}, "data"),
        prevent_initial_call=True,
    )
    def _show_inv_refresh_error(states: list | None):
        # Only failures reach the page error region; recovery shows in
        # the status line and the next navigation rewrites the store.
        state = (states or [None])[0]
        error = (state or {}).get("error") or ""
        if not error:
            raise PreventUpdate
        return [Error(error)]

    app.clientside_callback(
        """
        function(figure, relayout) {
            if (!figure || !figure.data) {
                return window.dash_clientside.no_update;
            }
            if (!relayout) {
                return figure;
            }
            const ends = {};
            for (const [key, value] of Object.entries(relayout)) {
                const dot = key.indexOf(".");
                if (dot < 0) continue;
                const axis = key.slice(0, dot);
                const rest = key.slice(dot + 1);
                if (!/^[xy]axis[0-9]*$/.test(axis)) continue;
                if (rest === "range" && Array.isArray(value)
                        && value.length === 2) {
                    ends[axis] = [value[0], value[1]];
                } else if (rest === "range[0]" || rest === "range[1]") {
                    const slot = ends[axis] || [null, null];
                    slot[rest === "range[0]" ? 0 : 1] = value;
                    ends[axis] = slot;
                }
            }
            const layout = Object.assign({}, figure.layout);
            let touched = false;
            for (const [axis, pair] of Object.entries(ends)) {
                if (pair[0] === null || pair[1] === null) continue;
                if (!(axis in layout)) continue;
                layout[axis] = Object.assign({}, layout[axis], {
                    range: pair, autorange: false,
                });
                touched = true;
            }
            return touched ? Object.assign({}, figure, {layout}) : figure;
        }
        """,
        Output("analysis-series-figure", "figure", allow_duplicate=True),
        Input("analysis-series-figure-store", "data"),
        State("analysis-series-figure", "relayoutData"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(hover, figure) {
            const no = window.dash_clientside.no_update;
            const trial = hover && hover.points && hover.points.length
                ? String(hover.points[0].customdata) : null;
            if (!figure || !figure.data) {
                return no;
            }
            const data = figure.data.map((trace) => {
                const mine = trace.customdata && trial
                    && String(trace.customdata[0]) === trial;
                const opacity = trial ? (mine ? 1 : 0.15) : null;
                const width = trial && mine ? 4 : 2;
                if (trace.opacity === opacity && trace.line
                        && trace.line.width === width) {
                    return trace;
                }
                return Object.assign({}, trace, {
                    opacity,
                    line: Object.assign({}, trace.line, {width}),
                });
            });
            return Object.assign({}, figure, {data});
        }
        """,
        Output("analysis-series-figure", "figure", allow_duplicate=True),
        Input("analysis-series-figure", "hoverData"),
        State("analysis-series-figure", "figure"),
        prevent_initial_call=True,
    )

    # -- Points: native brush + row-click selection -----------------------

    app.clientside_callback(
        """
        function(restyle, data) {
            const no = window.dash_clientside.no_update;
            if (!restyle || !data || !data.tks || !data.tks.length) {
                return no;
            }
            // Only a restyle that carries a brush acts; the selection
            // recolor's own line.color restyle must not clear a
            // row-click selection.
            if (!JSON.stringify(restyle).includes("constraintrange")) {
                return no;
            }
            const host = document.getElementById("inv-points-figure");
            const gd = host && (host.querySelector(".js-plotly-plot") || host);
            const full = gd && gd._fullData && gd._fullData[0];
            const dims = (full && full.dimensions) || [];
            if (!dims.length) {
                return no;
            }
            const cons = [];
            dims.forEach((dim) => {
                let cr = dim ? dim.constraintrange : null;
                if (cr === undefined || cr === null) return;
                if (!Array.isArray(cr)) cr = [cr];
                const ranges = cr.length && Array.isArray(cr[0]) ? cr : [cr];
                ranges.forEach((r) => {
                    if (Array.isArray(r) && r.length >= 2) {
                        cons.push([Math.min(r[0], r[1]), Math.max(r[0], r[1])]);
                    }
                });
            });
            const tks = [];
            (full.dimensions[0].values || []).forEach((_, line) => {
                const inside = dims.every((dim) => {
                    if (!dim.constraintrange) return true;
                    let cr = dim.constraintrange;
                    if (!Array.isArray(cr)) cr = [cr];
                    const ranges = cr.length && Array.isArray(cr[0])
                        ? cr : [cr];
                    const v = dim.values[line];
                    if (v !== v) return false;
                    return ranges.some((r) => {
                        if (!Array.isArray(r) || r.length < 2) return false;
                        return v >= Math.min(r[0], r[1])
                            && v <= Math.max(r[0], r[1]);
                    });
                });
                if (inside) tks.push(data.tks[line]);
            });
            tks.sort();
            return {tks: tks};
        }
        """,
        Output("inv-points-sel", "data"),
        Input("inv-points-figure", "restyleData"),
        State("inv-points-data", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(rows, data) {
            const no = window.dash_clientside.no_update;
            if (rows === undefined || rows === null) {
                return no;
            }
            const tks = (rows || [])
                .map((row) => row && row.tk)
                .filter(Boolean)
                .sort();
            return {tks: tks};
        }
        """,
        Output("inv-points-sel", "data", allow_duplicate=True),
        Input("inv-points-grid", "selectedRows"),
        State("inv-points-data", "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("inv-points-grid", "rowData"),
        Output("inv-points-note", "children"),
        Output("inv-points-clear", "style"),
        Output("inv-points-figure", "figure", allow_duplicate=True),
        Output("inv-points-data", "data", allow_duplicate=True),
        Output("inv-points-sel", "data", allow_duplicate=True),
        Input("inv-points-sel", "data"),
        Input("view-store", "data"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _filter_points_rows(
        selection: dict | None,
        view_doc: dict | None,
        pathname: str | None,
    ):
        # The table HIDES non-selected rows while the parcoords keeps
        # every line plotted — brushes fade natively, a selection only
        # recolors lines; axes never rescale. A scope change (member
        # flip) rebuilds the whole set and drops the selection.
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        scope_changed = "view-store.data" in triggered
        record, tray = _inv_context(pathname, view_doc)
        if record is None:
            raise PreventUpdate
        view = analysis.points_view_data(
            service.analysis_trials(record.project, tray),
            analysis.points_scalar_keys(service, record.project, tray),
            service.analysis_finals(record.project, tray),
            record.outcome,
        )
        picked = sorted(str(tk) for tk in (selection or {}).get("tks") or [])
        if scope_changed:
            picked = []
        rows = view["rows"]
        if picked:
            keep = set(picked)
            rows = [row for row in rows if row["tk"] in keep]
        note = f"{len(rows)} of {len(view['rows'])} trials shown" if picked else ""
        figure = figures.points_parcoords(view["dims"]) if scope_changed else no_update
        line_data = {"tks": view["tks"]} if scope_changed else no_update
        reset = {"tks": []} if scope_changed else no_update
        return (
            rows,
            note,
            {} if picked else {"display": "none"},
            figure,
            line_data,
            reset,
        )

    app.clientside_callback(
        """
        function(selection, data) {
            const no = window.dash_clientside.no_update;
            const host = document.getElementById("inv-points-figure");
            const gd = host && (host.querySelector(".js-plotly-plot") || host);
            if (!gd || !window.Plotly || !data || !data.tks
                    || !data.tks.length) {
                return no;
            }
            const VIRIDIS = ['#440154', '#482878', '#3e4989', '#31688e',
                '#26828e', '#1f9e89', '#35b779', '#6ece58', '#b5de2b',
                '#fde725'];
            const sel = new Set((selection && selection.tks) || []);
            const n = data.tks.length;
            let color;
            let colorscale = "Viridis";
            if (sel.size) {
                const stops = [[0, "#ececec"]];
                VIRIDIS.forEach((c, k) => {
                    stops.push([(k + 1) / VIRIDIS.length, c]);
                });
                colorscale = stops;
                const rank = new Map();
                data.tks.forEach((tk) => {
                    if (sel.has(tk)) {
                        rank.set(tk, ((rank.size + 1) / sel.size) * n);
                    }
                });
                color = data.tks.map((tk) => rank.get(tk) || 0);
            } else {
                color = data.tks.map((_, i) => i);
            }
            Plotly.restyle(gd, {
                "line.color": [color],
                "line.colorscale": [colorscale],
                "line.showscale": [false],
            });
            return no;
        }
        """,
        Output("inv-points-echo", "data"),
        Input("inv-points-sel", "data"),
        State("inv-points-data", "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("inv-points-sel", "data", allow_duplicate=True),
        Output("inv-points-grid", "selectedRows"),
        Input("inv-points-clear", "n_clicks"),
        prevent_initial_call=True,
    )
    def _clear_points_selection(_clicks: int | None):
        # Clearing restores every table row and drops the recolor: the
        # selection store empties and the grid's own selection resets.
        if not pressed_props(dash.callback_context):
            raise PreventUpdate
        return {"tks": []}, []

    # -- Search: the member filter ----------------------------------------

    @app.callback(
        Output("inv-search-grid", "rowData"),
        Output("inv-search-note", "children"),
        Input("inv-search-q", "value"),
        Input("view-store", "data"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _filter_inv_search(
        query: str | None,
        view_doc: dict | None,
        pathname: str | None,
    ):
        # The member scope rides the view document, so a member flip
        # re-narrows the rows exactly like a typed filter does.
        record, _tray = _inv_context(pathname, view_doc)
        if record is None:
            raise PreventUpdate
        member_ids = {str(m) for m in record.members}
        scoped = (view_doc or {}).get("inv", {}).get("member")
        if scoped and str(scoped) in member_ids:
            member_ids = {str(scoped)}
        rows = workspace.search_rows(
            [
                summary
                for summary in service.sweep_overview(record.project)
                if summary.sweep_id in member_ids
            ],
            record.project,
            str(record.id),
        )
        needle = str(query or "").strip().casefold()
        shown = [
            row for row in rows if not needle or needle in str(row["name"]).casefold()
        ]
        note = f"{len(shown)} of {len(rows)} member sweeps"
        return shown, note

    @app.callback(
        Output({"inv-edit-preview": ALL}, "children"),
        Output({"inv-edit-save": ALL}, "disabled"),
        Output({"inv-edit-grid": ALL}, "selectedRows"),
        Input({"inv-edit-state": ALL}, "data"),
        State({"inv-edit-grid": ALL}, "selectedRows"),
        State("url", "pathname"),
    )
    def _render_editor_preview(
        states: list | None, grid_selection: list | None, pathname: str | None
    ):
        if not states:
            raise PreventUpdate
        spec = parse_route(pathname)
        if spec.kind != "investigation-edit":
            raise PreventUpdate
        state = states[0] or {}
        picked = list(state.get("picked") or ())
        preview = service.investigation_preview(spec.object_id or "", picked)
        ready = bool(
            str(state.get("name") or "").strip()
            and state.get("factor")
            and state.get("outcome")
            and picked
        )
        # The freshly mounted grid takes the working selection; a
        # selection the user just made echoes back and must not be
        # rewritten (that would fight the click in progress).
        current_ids = sorted(
            {str(row.get("sweep_id")) for row in (grid_selection or [[]])[0] or []}
        )
        selection = (
            [no_update]
            if current_ids == picked
            else [[{"sweep_id": sweep_id} for sweep_id in picked]]
        )
        return (
            [workspace.editor_preview_panel(preview, state)],
            [not ready],
            selection,
        )

    @app.callback(
        Output({"inv-edit-state": ALL}, "data", allow_duplicate=True),
        Input({"inv-edit-grid": ALL}, "selectedRows"),
        State({"inv-edit-state": ALL}, "data"),
        prevent_initial_call=True,
    )
    def _edit_editor_members(rows: list | None, states: list | None):
        if not states:
            raise PreventUpdate
        state = dict(states[0] or {})
        picked = sorted({str(row["sweep_id"]) for row in (rows or [[]])[0] or []})
        if picked == state.get("picked"):
            raise PreventUpdate
        state["picked"] = picked
        return [state]

    @app.callback(
        Output({"inv-edit-state": ALL}, "data", allow_duplicate=True),
        Input({"inv-edit-name": ALL}, "value"),
        State({"inv-edit-state": ALL}, "data"),
        prevent_initial_call=True,
    )
    def _edit_editor_name(names: list | None, states: list | None):
        if not states:
            raise PreventUpdate
        state = dict(states[0] or {})
        name = str((names or [""])[0] or "").strip()
        if name == state.get("name"):
            raise PreventUpdate
        state["name"] = name
        return [state]

    @app.callback(
        Output({"inv-edit-state": ALL}, "data", allow_duplicate=True),
        Input({"inv-edit-factor": ALL}, "value"),
        Input({"inv-edit-outcome": ALL}, "value"),
        State({"inv-edit-state": ALL}, "data"),
        prevent_initial_call=True,
    )
    def _edit_editor_body(
        factors: list | None, outcomes: list | None, states: list | None
    ):
        if not states:
            raise PreventUpdate
        state = dict(states[0] or {})
        factor = (factors or [None])[0]
        outcome = (outcomes or [None])[0]
        if factor == state.get("factor") and outcome == state.get("outcome"):
            raise PreventUpdate
        state["factor"] = factor
        state["outcome"] = outcome
        return [state]

    @app.callback(
        Output({"inv-edit-factor": ALL}, "options"),
        Output({"inv-edit-outcome": ALL}, "options"),
        Input({"inv-edit-state": ALL}, "data"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _load_editor_options(states: list | None, pathname: str | None):
        if not states:
            raise PreventUpdate
        spec = parse_route(pathname)
        if spec.kind != "investigation-edit":
            raise PreventUpdate
        state = states[0] or {}
        preview = service.investigation_preview(
            spec.object_id or "", list(state.get("picked") or ())
        )
        return [workspace.editor_factor_options(preview)], [
            workspace.editor_outcome_options(preview)
        ]

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Output({"inv-edit-state": ALL}, "data", allow_duplicate=True),
        Output({"inv-edit-grid": ALL}, "rowData", allow_duplicate=True),
        Output({"inv-edit-message": ALL}, "children", allow_duplicate=True),
        Input({"inv-edit-save": ALL}, "n_clicks"),
        State({"inv-edit-state": ALL}, "data"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _save_editor(_clicks: list | None, states: list | None, pathname: str | None):
        if not states or not pressed_props(dash.callback_context):
            raise PreventUpdate
        spec = parse_route(pathname)
        if spec.kind != "investigation-edit":
            raise PreventUpdate
        state = states[0] or {}
        project = spec.object_id or ""
        picked = list(state.get("picked") or ())
        if spec.sub_id is None:
            name = str(state.get("name") or "").strip()
            factor = state.get("factor")
            outcome = state.get("outcome")
            if not (name and factor and outcome and picked):
                return (
                    no_update,
                    no_update,
                    no_update,
                    no_update,
                    [
                        workspace.action_message(
                            False,
                            "A name, a factor, an outcome, and at least one "
                            "member are required.",
                        )
                    ],
                )
            try:
                record = service.create_investigation(
                    project, name, factor, outcome, members=picked
                )
            except CurationRejectedError as error:
                return (
                    no_update,
                    no_update,
                    no_update,
                    no_update,
                    [workspace.action_message(False, str(error))],
                )
            saved = sorted({str(sweep) for sweep in record.members})
            return (
                f"{ROUTES_BASE}/project/{project}/investigation/{record.id}",
                "",
                [{**state, "picked": saved, "saved": saved}],
                [no_update],
                [""],
            )
        try:
            record = service.set_investigation_members(spec.sub_id, picked)
        except CurationRejectedError as error:
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                [workspace.action_message(False, str(error))],
            )
        saved = [str(sweep) for sweep in record.members]
        return (
            no_update,
            no_update,
            [{**state, "picked": saved, "saved": saved}],
            [workspace.editor_rows(service.sweep_overview(project), saved, project)],
            [
                workspace.action_message(
                    True, f"Saved — {len(saved)} members in {record.name}."
                )
            ],
        )

    @app.callback(
        Output({"inv-edit-state": ALL}, "data", allow_duplicate=True),
        Output({"inv-edit-grid": ALL}, "selectedRows", allow_duplicate=True),
        Output({"inv-edit-message": ALL}, "children", allow_duplicate=True),
        Input({"inv-edit-discard": ALL}, "n_clicks"),
        State({"inv-edit-state": ALL}, "data"),
        prevent_initial_call=True,
    )
    def _discard_editor(_clicks: list | None, states: list | None):
        if not states or not pressed_props(dash.callback_context):
            raise PreventUpdate
        state = states[0] or {}
        saved = list(state.get("saved") or ())
        return (
            [{**state, "picked": saved}],
            [[{"sweep_id": sweep_id} for sweep_id in saved]],
            [""],
        )

    # -- Analysis tabs inside the workspace ------------------------------

    @app.callback(
        Output("view-store", "data"),
        Output("analysis-message-store", "data"),
        Input("url", "pathname"),
        Input("url", "search"),
        Input("project-store", "data"),
        State("view-store", "data"),
    )
    def _hydrate_workspace_state(
        pathname: str | None,
        search: str | None,
        project: str | None,
        current: dict | None,
    ):
        # Shell-only outputs: this fires on every navigation, and Dash
        # raises ReferenceError when a dispatched callback writes a
        # component the current page does not mount (jernerics-8c9).
        # Sole hydration writer: a ?sel= token lands as scope dimensions
        # over whatever the ?view= document hydrated, include flags and
        # focus survive both.
        scope, tray_error = analysis.hydrate_tray(
            service, project, pathname, search, (current or {}).get("scope")
        )
        view, view_error = analysis.hydrate_view(pathname, search, current)
        message = tray_error or view_error
        doc = view if view is not None else current
        if scope is not None:
            base = doc if doc is not None else analysis.default_view_state()
            doc = analysis.edited_view(
                base, {"scope": {**base.get("scope", {}), **scope}}
            )
        if doc is None or doc == current:
            return no_update, message or ""
        return doc, message or ""

    @app.callback(
        Output("url", "search"),
        Input("url", "pathname"),
        Input("view-store", "data"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _sync_selection_url(
        pathname: str | None,
        view_doc: dict | None,
        current_search: str | None,
    ):
        """Sole owner of ``url.search``: mints ``?view=`` from view edits
        on the workspace page and drops it when navigating away. Only
        shell-resident ids, so it can fire on any page."""
        triggered = {item["prop_id"] for item in dash.callback_context.triggered}
        target = analysis.synced_search(
            pathname,
            view_doc,
            current_search,
            url_navigated="url.pathname" in triggered,
        )
        if target is None:
            raise PreventUpdate
        return target

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-family-grid", "selectedRows"),
        Input("analysis-expand", "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_analysis_tray(
        family_rows: list[dict] | None,
        expand_flags: list[str] | None,
        current: dict | None,
    ):
        triggered = dash.callback_context.triggered_prop_ids
        scope = analysis.tray_from_edit(
            None,
            family_rows,
            expand_flags,
            (current or {}).get("scope"),
            sweep_edited=False,
            family_edited="analysis-family-grid.selectedRows" in triggered,
            expand_edited="analysis-expand.value" in triggered,
        )
        doc = analysis.edited_view(current, {"scope": scope})
        # AG Grid echoes its programmatic selectedRows back on mount, and
        # a restore replays the stored scope; neither is an edit.
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input("analysis-include", "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_include(values: list[str] | None, current: dict | None):
        doc = analysis.view_from_include(current, values)
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output({"analysis-error": ALL}, "children"),
        Input("analysis-message-store", "data"),
    )
    def _show_analysis_message(message: str | None):
        # One entry per matched pattern component; a clear stays an event.
        return [Error(message)] if message else [no_update]

    @app.callback(
        Output("view-store", "data", allow_duplicate=True),
        Input({"analysis-tabs": ALL}, "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_view_state(active: list | None, current: dict | None):
        doc = analysis.view_from_controls(
            current,
            active=(active or [None])[0],
            keys=None,
            mode=None,
            reduction=None,
            color=None,
            facet=None,
            contour_x=None,
            contour_y=None,
            edited={"active"},
        )
        # Hydration pushes state to the controls and their echo lands
        # here; an unchanged document is not an edit.
        if doc == (current or {}):
            raise PreventUpdate
        return doc

    @app.callback(
        Output({"analysis-tabs": ALL}, "value"),
        Output("analysis-include", "value"),
        Output("analysis-expand", "value"),
        Input("view-store", "data"),
    )
    def _sync_view_controls(doc: dict | None):
        # The include and expand checklists ride along so one store
        # write is one sync POST; the analysis pickers return with the
        # sweep-scope views (jernerics-g5rw.9).
        return (
            [analysis.control_values(doc, {})[0]],
            analysis.include_values(doc),
            analysis.expand_values(doc),
        )

    # -- Scroll preservation across refreshes ----------------------------

    app.clientside_callback(
        """
        function(clicks, tick) {
            const grids = [];
            document.querySelectorAll(".ag-body-viewport").forEach(
                (viewport) => {
                    let root = viewport;
                    while (root && root !== document.body && !root.id) {
                        root = root.parentElement;
                    }
                    if (!root || !root.id) return;
                    grids.push({
                        id: root.id,
                        top: viewport.scrollTop,
                        left: viewport.scrollLeft,
                    });
                }
            );
            return {
                x: window.scrollX,
                y: window.scrollY,
                grids: grids,
            };
        }
        """,
        Output("scroll-restore-store", "data"),
        Input({"analysis-refresh": ALL}, "n_clicks"),
        Input("poll", "n_intervals"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(refreshState, overviewRendered, state) {
            if (!state || !state.grids) {
                return window.dash_clientside.no_update;
            }
            const restore = () => {
                window.scrollTo(state.x || 0, state.y || 0);
                for (const grid of state.grids) {
                    const root = document.getElementById(grid.id);
                    if (!root) continue;
                    const viewport = root.querySelector(".ag-body-viewport");
                    if (!viewport) continue;
                    viewport.scrollTop = grid.top || 0;
                    viewport.scrollLeft = grid.left || 0;
                }
            };
            window.requestAnimationFrame(() => window.requestAnimationFrame(restore));
            return window.dash_clientside.no_update;
        }
        """,
        Output("scroll-restore-store", "data", allow_duplicate=True),
        Input({"analysis-refresh-store": ALL}, "data"),
        Input("workspace-overview", "children"),
        State("scroll-restore-store", "data"),
        prevent_initial_call=True,
    )

    # -- Artifact viewer (jernerics-h5d.14) -------------------------------

    @app.callback(
        Output("artifact-rows-grid", "dashGridOptions"),
        Input("artifact-quick-filter", "value"),
        prevent_initial_call=True,
    )
    def _filter_artifact_rows(text: str | None):
        return components.grid_options(quickFilterText=text or "")
