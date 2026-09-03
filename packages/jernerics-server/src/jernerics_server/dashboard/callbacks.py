"""Navigation, project picker, overview selection, and polling callbacks.

The dashboard is a router over server-rendered pages: every URL renders
its page whole through :func:`page_content`, link clicks reload it with
new query parameters, and callbacks exist where live data or client
state genuinely requires them (polling, the overview selection bar,
investigation regions, the artifact viewer). Page data flows exclusively
through the shared QueryService (wrapped by DashboardService) — there is
no second SQL layer.
"""

import hashlib
import json
import time
from typing import Any
from uuid import UUID

import dash
from dash import ALL, Input, Output, State, html, no_update
from dash.exceptions import PreventUpdate

from . import analysis, artifacts, components, figures, layout, workspace
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
    view_doc: dict | None = None,
    search: str | None = None,
) -> tuple[Any, bool]:
    """(page, poll enabled) for a URL, with live data.

    ``poll enabled`` is True only while the shown page's work is
    incomplete: any sweep in the overview's visible scope, or the
    investigation surface's own refresh intent.
    """
    spec = parse_route(pathname)
    now = time.time_ns() if now_ns is None else now_ns
    if spec.kind == "project":
        return layout.project_page(service.project_catalog(), now), False
    if spec.kind == "workspace":
        project = spec.object_id or ""
        url = workspace.parse_overview_url(search)
        return (
            workspace.overview_page(service, project, url=url, now_ns=now),
            workspace.overview_polls(service, project, url),
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


def _event_field(event: Any, name: str) -> Any:
    """One ``triggered`` entry field; Dash has shipped both dict and
    attribute event shapes."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def pressed_props(context: Any) -> set[str]:
    """Prop ids of the controls a user actually pressed: a re-render
    remounts controls and re-fires their callbacks with click counts of
    None, which must never act (jernerics-gk6)."""
    return {
        str(_event_field(event, "prop_id"))
        for event in context.triggered or ()
        if _event_field(event, "value")
    }


def project_options(projects: list[str]) -> list[dict[str, str]]:
    return [{"label": project, "value": project} for project in projects]


def overview_facts(
    service: DashboardService,
    project: str,
    *,
    scope_all: bool = False,
) -> dict[str, Any]:
    """Canonical overview facts: one stored-facts row per sweep in the
    visible scope — never the rendered tree, so relative-time strings
    cannot churn the digest (jernerics-l4k)."""
    summaries = service.sweep_overview(project)
    visible = summaries if scope_all else workspace.active_sweeps(summaries)
    return {
        "project": project,
        "scope_all": scope_all,
        "sweeps": sorted(
            (
                str(summary.sweep_id),
                summary.name,
                summary.state,
                summary.trials,
                summary.trials_complete,
                summary.best_objective,
                summary.failed,
                summary.stale,
                summary.latest_submitted_ns,
                summary.archived_ns,
                summary.invalid_ns,
                summary.incomplete,
            )
            for summary in visible
        ),
    }


def overview_content(
    service: DashboardService,
    project: str,
    *,
    scope_all: bool = False,
) -> tuple[dict[str, Any], str]:
    """(facts, digest) of the visible overview scope; the digest covers
    stored facts only, never the rendered children."""
    facts = overview_facts(service, project, scope_all=scope_all)
    return facts, _content_digest(facts)


def register_callbacks(app: dash.Dash, service: DashboardService) -> None:
    @app.callback(
        Output("page-container", "children"),
        Output("poll", "disabled"),
        Output("view-store", "data", allow_duplicate=True),
        Output("route-store", "data"),
        Output("overview-digest-store", "data"),
        Input("url", "pathname"),
        Input("poll", "n_intervals"),
        State("view-store", "data"),
        State("route-store", "data"),
        State("overview-digest-store", "data"),
        State("url", "search"),
        prevent_initial_call="initial_duplicate",
    )
    def _render_page(
        pathname: str | None,
        _tick: int | None,
        view_doc: dict | None,
        rendered_route: str | None,
        digest_doc: dict | None,
        search: str | None,
    ):
        spec = parse_route(pathname)
        kind = spec.kind
        project = spec.object_id
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        ticked = "poll.n_intervals" in triggered
        # A url.search rewrite re-fires the pathname watcher with the
        # route unchanged; re-rendering would remount the page and
        # orphan every region under it for nothing.
        if (
            not ticked
            and set(triggered) == {"url.pathname"}
            and (pathname == rendered_route)
        ):
            raise PreventUpdate
        if ticked and kind == "workspace":
            # A tick re-renders the overview only when a stored fact in
            # the visible scope changed; relative time never counts.
            url = workspace.parse_overview_url(search)
            facts, digest = overview_content(
                service, project or "", scope_all=url.scope_all
            )
            polls = any(entry[-1] for entry in facts["sweeps"])
            if digest == (digest_doc or {}).get("digest"):
                # An unchanged tick must ship nothing at all: a no_update
                # multi-response still wakes every watcher of the store.
                raise PreventUpdate
            page, _ = page_content(pathname, service, search=search)
            return page, not polls, no_update, no_update, {"digest": digest}
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
                            spec.sub_id or ""
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
        page, polls = page_content(pathname, service, view_doc=view_doc, search=search)
        # A rendered page remounts the overview, so its content digest
        # from the previous mount is void.
        return page, not polls, no_update, pathname, None

    @app.callback(
        Output("poll", "disabled", allow_duplicate=True),
        Output("poll-gate-facts-store", "data"),
        Input("url", "pathname"),
        Input("url", "search"),
        Input("view-store", "data"),
        State("project-store", "data"),
        State("poll-gate-facts-store", "data"),
        prevent_initial_call=True,
    )
    def _gate_workspace_poll(
        pathname: str | None,
        search: str | None,
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
            "search": search or "",
            "scope": analysis.scope_dims(doc.get("scope")),
            "auto_refresh": doc.get("auto_refresh"),
            "focus": doc.get("focus"),
            "inv": doc.get("inv"),
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if (
            "url.pathname" not in triggered
            and "url.search" not in triggered
            and (facts or {}).get("project") == desired["project"]
            and (facts or {}).get("search") == desired["search"]
            and (facts or {}).get("scope") == desired["scope"]
            and (facts or {}).get("auto_refresh") == desired["auto_refresh"]
            and (facts or {}).get("focus") == desired["focus"]
            and (facts or {}).get("inv") == desired["inv"]
        ):
            raise PreventUpdate
        spec = parse_route(pathname)
        kind = spec.kind
        if kind == "investigation":
            # The investigation Series view polls only while its own
            # auto-refresh intent is on and the member scope still has
            # incomplete work.
            if not (project and doc.get("auto_refresh")):
                return True, desired
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
        if kind != "workspace":
            raise PreventUpdate
        url = workspace.parse_overview_url(search)
        return not workspace.overview_polls(service, spec.object_id or "", url), desired

    # -- Overview selection bar (clientside; the checkboxes only exist
    # on the overview page) ----------------------------------------------

    app.clientside_callback(
        """
        function(values) {
            const entries = (dash_clientside.callback_context.inputs_list || [])
                .flat()
                .filter((entry) => entry && entry.id && (entry.value || []).length);
            const picked = entries
                .map((entry) => String(entry.id["sel-sweep"]))
                .sort();
            const count = picked.length;
            const target =
                count === 0
                    ? "#"
                    : window.location.pathname
                      + "/investigation/new?sweeps=" + picked.join(",");
            return [
                count === 0,
                count === 0
                    ? ""
                    : count + (count === 1 ? " sweep selected" : " sweeps selected"),
                target,
            ];
        }
        """,
        Output("selbar", "hidden"),
        Output("sel-count", "children"),
        Output("sel-create", "href"),
        Input({"sel-sweep": ALL}, "value"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(clicks, values) {
            if (!clicks) {
                return window.dash_clientside.no_update;
            }
            return (values || []).map(() => []);
        }
        """,
        Output({"sel-sweep": ALL}, "value", allow_duplicate=True),
        Input("sel-clear", "n_clicks"),
        State({"sel-sweep": ALL}, "value"),
        prevent_initial_call=True,
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
        if spec.kind in ("workspace", "investigation", "investigation-edit"):
            if spec.object_id == current:
                raise PreventUpdate
            return spec.object_id
        raise PreventUpdate

    @app.callback(
        Output("url", "pathname", allow_duplicate=True),
        Output("url", "search", allow_duplicate=True),
        Input("compare-members-grid", "cellClicked"),
        State("url", "pathname"),
        prevent_initial_call=True,
    )
    def _open_member_sweep(click: dict | None, pathname: str | None):
        # A row click on the member inventory opens that sweep's page.
        spec = parse_route(pathname)
        if spec.kind != "investigation" or not isinstance(click, dict):
            raise PreventUpdate
        row_id = click.get("rowId")
        if not row_id:
            raise PreventUpdate
        return (
            f"{ROUTES_BASE}/project/{spec.object_id or ''}/sweep/{row_id}",
            "",
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
        Output({"analysis-error": ALL}, "children"),
        Input("analysis-message-store", "data"),
    )
    def _show_analysis_message(message: str | None):
        # One entry per matched pattern component; a clear stays an event.
        return [Error(message)] if message else [no_update]

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
        function(refreshState, state) {
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
        State("scroll-restore-store", "data"),
        prevent_initial_call=True,
    )

    # -- Artifact listing and viewer (jernerics-h5d.14) ------------------

    @app.callback(
        Output("url", "pathname"),
        Input("artifact-grid", "cellClicked"),
        prevent_initial_call=True,
    )
    def _open_artifact(click: dict | None):
        row_id = click.get("rowId") if isinstance(click, dict) else None
        try:
            artifact_id = UUID(str(row_id))
        except ValueError:
            raise PreventUpdate from None
        return artifacts.viewer_href(str(artifact_id))

    @app.callback(
        Output("artifact-rows-grid", "dashGridOptions"),
        Input("artifact-quick-filter", "value"),
        prevent_initial_call=True,
    )
    def _filter_artifact_rows(text: str | None):
        return components.grid_options(quickFilterText=text or "")
