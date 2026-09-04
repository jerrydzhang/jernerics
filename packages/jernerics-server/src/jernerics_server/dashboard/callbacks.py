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

import dash
from dash import ALL, Input, Output, State, no_update
from dash.exceptions import PreventUpdate

from . import (
    analysis,
    artifacts,
    components,
    exceptions,
    figures,
    layout,
    page,
    sweep,
    sweep_views,
    workspace,
)
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
    if spec.kind == "investigations":
        return (
            workspace.investigations_index_page(service, spec.object_id or "", now),
            False,
        )
    if spec.kind == "investigation":
        investigation_id = spec.sub_id or ""
        try:
            page = workspace.investigation_page(
                service,
                spec.object_id or "",
                investigation_id,
                search=search,
                now_ns=now,
            )
        except CurationRejectedError:
            return layout.missing_object_page("investigation", investigation_id), False
        return page, False
    if spec.kind == "exceptions":
        return (
            exceptions.exceptions_page(
                service, spec.object_id or "", search=search, now_ns=now
            ),
            False,
        )
    if spec.kind == "investigation-edit":
        try:
            page = workspace.investigation_edit_page(
                service,
                spec.object_id or "",
                spec.sub_id,
                analysis.seed_sweeps_from_search(search),
                now_ns=now,
            )
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
            view=sweep_views.view_from_search(search),
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
            query = workspace.investigation_query(search)
            doc = view_doc or analysis.default_view_state()
            polls = bool(doc.get("auto_refresh")) and query["view"] == "series"
            if polls:
                try:
                    record = service.investigation_detail(
                        spec.sub_id or ""
                    ).investigation
                except CurationRejectedError:
                    polls = False
                else:
                    tray, _scoped = analysis.investigation_scope_state(
                        record.members, query["member"]
                    )
                    polls = service.analysis_scope_incomplete(project or "", tray)
            flip = analysis.auto_refresh_flip(view_doc, polls)
            return (
                no_update,
                not polls,
                no_update if flip is None else flip,
                no_update,
                no_update,
            )
        if ticked and kind == "sweep":
            polls = service.sweep_incomplete(spec.sub_id or "")
            return no_update, not polls, no_update, no_update, no_update
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
        State("poll-gate-facts-store", "data"),
        prevent_initial_call=True,
    )
    def _gate_workspace_poll(
        pathname: str | None,
        search: str | None,
        view_doc: dict | None,
        facts: dict | None,
    ):
        # The router only fires on navigation and ticks; view edits must
        # re-evaluate the interval themselves — but only when a fact the
        # gate consumes actually changed, never on every view-store write.
        spec = parse_route(pathname)
        desired = {
            "project": spec.object_id,
            "search": search or "",
            "auto_refresh": (view_doc or {}).get("auto_refresh"),
        }
        triggered = {str(prop) for prop in dash.callback_context.triggered_prop_ids}
        if (
            "url.pathname" not in triggered
            and "url.search" not in triggered
            and (facts or {}).get("project") == desired["project"]
            and (facts or {}).get("search") == desired["search"]
            and (facts or {}).get("auto_refresh") == desired["auto_refresh"]
        ):
            raise PreventUpdate
        kind = spec.kind
        if kind == "investigation":
            # The investigation Series view polls only while its own
            # auto-refresh intent is on and the member scope still has
            # incomplete work.
            query = workspace.investigation_query(search)
            if not (spec.object_id and desired["auto_refresh"]):
                return True, desired
            try:
                members = service.investigation_detail(
                    spec.sub_id or ""
                ).investigation.members
            except CurationRejectedError:
                return True, desired
            tray, _scoped = analysis.investigation_scope_state(members, query["member"])
            return (
                not service.analysis_scope_incomplete(spec.object_id, tray),
                desired,
            )
        if kind == "sweep":
            sweep_id = spec.sub_id or ""
            return (not service.sweep_incomplete(sweep_id), desired)
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

    # -- Investigation views, member scope, and analysis regions ----------

    def _inv_context(
        pathname: str | None, search: str | None
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
        query = workspace.investigation_query(search)
        tray, _scoped = analysis.investigation_scope_state(
            record.members, query["member"]
        )
        return record, tray

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
        # The investigation Series controls carry the whole surviving
        # control surface of the view document.
        edited = analysis.edited_fields(dash.callback_context.triggered_prop_ids)
        if not edited or len(edited) > 2:
            raise PreventUpdate
        doc = analysis.view_from_controls(
            current,
            keys=keys,
            mode=mode,
            reduction=reduction,
            color=color,
            facet=facet,
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
            values[0],
            values[1],
            values[2],
            values[3],
            values[4],
            values[5],
            values[6],
        )

    @app.callback(
        Output("analysis-series-data", "data"),
        Output("analysis-updated", "children"),
        Output("analysis-series-status", "children", allow_duplicate=True),
        Input("view-store", "data"),
        Input({"analysis-refresh": ALL}, "n_clicks"),
        Input("poll", "n_intervals"),
        State("url", "pathname"),
        State("url", "search"),
        State("analysis-series-data", "data"),
        prevent_initial_call=True,
    )
    def _load_inv_series_snapshot(
        view_doc: dict | None,
        _clicks: int | None,
        _tick: int | None,
        pathname: str | None,
        search: str | None,
        snapshot: dict | None,
    ):
        # Only the investigation's scope, a manual refresh, an enabled
        # poll, or a series-view activation fetch series data; a usable
        # snapshot renders from the store instead. Outputs stay
        # page-local: jernerics-8c9 forbids mixing the shell's view
        # store into a callback the shell can fire, so the
        # auto-refresh flip lives in the router callback instead.
        doc = view_doc or analysis.default_view_state()
        if workspace.investigation_query(search)["view"] != "series":
            raise PreventUpdate
        record, tray = _inv_context(pathname, search)
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
            data, updated = analysis.series_data_outputs(
                service, record.project, tray, doc, now
            )
        except Exception as error:
            return (*analysis.series_data_failure(error, now),)
        return data, updated, no_update

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
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _render_inv_series(
        view_doc: dict | None,
        snapshot: dict | None,
        pathname: str | None,
        search: str | None,
    ):
        # Presentation rebuilds from the stored snapshot: view-only
        # edits issue zero reads, added keys fetch only the missing
        # ones, scope/reduction changes rebuild.
        doc = view_doc or analysis.default_view_state()
        if workspace.investigation_query(search)["view"] != "series":
            raise PreventUpdate
        record, tray = _inv_context(pathname, search)
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
        Input({"context-filter": dash.ALL}, "value"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _edit_context_filters(values: list | None, current: dict | None):
        # Re-rendered filter dropdowns fire as additions; the resolved
        # id names the edited dimension and its value comes from the
        # ALL input list (dash 4 child-id resolution).
        dimension, control = pattern_trigger(dash.callback_context)
        if control != "context-filter" or dimension is None:
            raise PreventUpdate
        value = pattern_input_value(
            dash.callback_context.inputs_list, 0, "context-filter", dimension
        )
        doc = analysis.view_from_context_filter(current, dimension, value)
        if doc == (current or {}):
            raise PreventUpdate
        return doc

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
        Input("inv-points-sel", "data"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _filter_points_rows(
        selection: dict | None,
        pathname: str | None,
        search: str | None,
    ):
        # The table HIDES non-selected rows while the parcoords keeps
        # every line plotted — brushes fade natively, a selection only
        # recolors lines; axes never rescale. The member scope rides
        # the URL: a scope change remounts the page with a fresh set.
        record, tray = _inv_context(pathname, search)
        if record is None:
            raise PreventUpdate
        view = analysis.points_view_data(
            service.analysis_trials(record.project, tray),
            analysis.points_scalar_keys(service, record.project, tray),
            service.analysis_finals(record.project, tray),
            record.outcome,
        )
        picked = sorted(str(tk) for tk in (selection or {}).get("tks") or [])
        rows = view["rows"]
        if picked:
            keep = set(picked)
            rows = [row for row in rows if row["tk"] in keep]
        note = f"{len(rows)} of {len(view['rows'])} trials shown" if picked else ""
        return (
            rows,
            note,
            {} if picked else {"display": "none"},
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
        Output("inv-search-results", "children"),
        Output("inv-search-note", "children"),
        Input("inv-search-q", "value"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _filter_inv_search(
        query: str | None,
        pathname: str | None,
        search: str | None,
    ):
        # The member scope rides the URL; a member flip remounts the
        # page, a typed filter narrows the rendered rows in place.
        record, _tray = _inv_context(pathname, search)
        if record is None:
            raise PreventUpdate
        member_ids = {str(m) for m in record.members}
        scoped = workspace.investigation_query(search)["member"]
        if scoped and str(scoped) in member_ids:
            member_ids = {str(scoped)}
        now = time.time_ns()
        rows, _shown, _total = workspace.search_rows(
            [
                summary
                for summary in service.sweep_overview(record.project)
                if summary.sweep_id in member_ids
            ],
            record.project,
            str(record.id),
            str(query or "").strip().casefold(),
            now,
        )
        table = page.scroll_table(
            [
                page.head_cell("Sweep"),
                page.head_cell("Status"),
                page.head_cell("Trials", numeric=True),
                page.head_cell("Last activity", numeric=True),
            ],
            rows,
            sortable=True,
        )

    @app.callback(
        Output({"inv-edit-preview": ALL}, "children"),
        Output({"inv-edit-pick": ALL}, "value"),
        Output({"inv-edit-mode": ALL}, "children"),
        Input({"inv-edit-state": ALL}, "data"),
        State({"inv-edit-pick": ALL}, "id"),
        State({"inv-edit-pick": ALL}, "value"),
        State({"inv-edit-mode": ALL}, "id"),
        State("url", "pathname"),
    )
    def _render_editor_preview(
        states: list | None,
        pick_ids: list | None,
        pick_values: list | None,
        mode_ids: list | None,
        pathname: str | None,
    ):
        if not states:
            raise PreventUpdate
        spec = parse_route(pathname)
        if spec.kind != "investigation-edit":
            raise PreventUpdate
        state = states[0] or {}
        picked = list(state.get("picked") or ())
        preview = service.investigation_preview(spec.object_id or "", picked)
        picked_set = set(picked)
        # A freshly mounted table takes the working selection in its
        # checkboxes; a set the user just clicked echoes back and must
        # not be rewritten (that would fight the click in progress).
        mounted = [str(item["inv-edit-pick"]) for item in pick_ids or []]
        current = {
            sweep_id
            for sweep_id, values in zip(mounted, pick_values or [], strict=False)
            for flag in ([values] if not isinstance(values, list) else values)
            if flag
        }
        if current == picked_set:
            values = [no_update] * len(mounted)
        else:
            values = [
                [sweep_id] if sweep_id in picked_set else [] for sweep_id in mounted
            ]
        members_label: list[Any] = [no_update] * len(mode_ids or [])
        for index, item in enumerate(mode_ids or []):
            if item.get("inv-edit-mode") == "members":
                members_label[index] = f"Members ({len(picked)})"
        return (
            [workspace.editor_preview_panel(preview, state)],
            values,
            members_label,
        )

    @app.callback(
        Output({"inv-edit-state": ALL}, "data", allow_duplicate=True),
        Input({"inv-edit-pick": ALL}, "value"),
        State({"inv-edit-pick": ALL}, "id"),
        State({"inv-edit-state": ALL}, "data"),
        prevent_initial_call=True,
    )
    def _edit_editor_members(
        values: list | None, pick_ids: list | None, states: list | None
    ):
        if not states:
            raise PreventUpdate
        state = dict(states[0] or {})
        picked = sorted(
            str(item["inv-edit-pick"])
            for item, ticked in zip(pick_ids or [], values or [], strict=False)
            if ticked
        )
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
                    [no_update],
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
                    [no_update],
                    [workspace.action_message(False, str(error))],
                )
            saved = sorted({str(sweep) for sweep in record.members})
            return (
                f"{ROUTES_BASE}/project/{project}/investigation/{record.id}",
                "",
                [{**state, "picked": saved, "saved": saved}],
                [""],
            )
        if not picked:
            return (
                no_update,
                no_update,
                [no_update],
                [workspace.action_message(False, "At least one member is required.")],
            )
        try:
            record = service.set_investigation_members(spec.sub_id, picked)
        except CurationRejectedError as error:
            return (
                no_update,
                no_update,
                [no_update],
                [workspace.action_message(False, str(error))],
            )
        saved = [str(sweep) for sweep in record.members]
        return (
            no_update,
            no_update,
            [{**state, "picked": saved, "saved": saved}],
            [
                workspace.action_message(
                    True, f"Saved — {len(saved)} members in {record.name}."
                )
            ],
        )

    @app.callback(
        Output({"inv-edit-state": ALL}, "data", allow_duplicate=True),
        Output({"inv-edit-pick": ALL}, "value", allow_duplicate=True),
        Output({"inv-edit-message": ALL}, "children", allow_duplicate=True),
        Input({"inv-edit-discard": ALL}, "n_clicks"),
        State({"inv-edit-state": ALL}, "data"),
        State({"inv-edit-pick": ALL}, "id"),
        prevent_initial_call=True,
    )
    def _discard_editor(
        _clicks: list | None,
        states: list | None,
        pick_ids: list | None,
    ):
        if not states or not pressed_props(dash.callback_context):
            raise PreventUpdate
        state = states[0] or {}
        saved = list(state.get("saved") or ())
        saved_set = set(saved)
        return (
            [{**state, "picked": saved}],
            [
                [item["inv-edit-pick"]]
                if str(item["inv-edit-pick"]) in saved_set
                else []
                for item in pick_ids or []
            ],
            [""],
        )

    # -- Artifact viewer (jernerics-h5d.14) -------------------------------

    @app.callback(
        Output("artifact-rows-grid", "dashGridOptions"),
        Input("artifact-quick-filter", "value"),
        prevent_initial_call=True,
    )
    def _filter_artifact_rows(text: str | None):
        return components.grid_options(quickFilterText=text or "")

    # -- Exceptions triage -------------------------------------------------
    @app.callback(
        Output("exc-groupsets", "children"),
        Output("exc-note", "children"),
        Output("exc-selection-count", "children"),
        Input("exc-selection-store", "data"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _drive_exceptions_triage(
        action: dict | None,
        pathname: str | None,
        search: str | None,
    ):
        # The store write lands only from the page's Mark invalid press
        # (the clientside writer collects the DOM selection); every
        # other store echo is a no-op. The roll-up re-renders so freshly
        # invalidated sweeps leave it, and the selection restarts empty.
        spec = parse_route(pathname)
        if spec.kind != "exceptions":
            raise PreventUpdate
        cleared = "0 sweeps selected"
        sweeps = [str(s) for s in (action or {}).get("sweeps") or []]
        if not sweeps:
            return (
                no_update,
                exceptions.action_note(
                    False,
                    "Select sweeps first — actions apply to checked failed sweeps.",
                ),
                cleared,
            )
        ok, report = apply_curation(
            service, "invalid", sweeps, str((action or {}).get("reason") or "")
        )
        return (
            exceptions.rollup(
                service,
                spec.object_id or "",
                scope_all=exceptions.scope_all(search),
                now_ns=time.time_ns(),
                visible_mode=str((action or {}).get("mode") or "cause"),
            ),
            exceptions.action_note(ok, report),
            cleared,
        )

    # The clientside store writer: the checked boxes live only in the page DOM.
    app.clientside_callback(
        """
        function(clicks, reason) {
            if (!clicks) {
                return window.dash_clientside.no_update;
            }
            const sweeps = Array.from(
                document.querySelectorAll(".sel-sweep input:checked"),
                (box) => box.name
            );
            const active = document.querySelector("#exc-mode-seg .gmode.on");
            return {
                sweeps: sweeps,
                reason: reason || "",
                mode: (active && active.dataset.mode) || "cause",
            };
        }
        """,
        Output("exc-selection-store", "data"),
        Input("exc-mark-invalid", "n_clicks"),
        State("exc-reason", "value"),
        prevent_initial_call=True,
    )

    # -- Investigation Compare: the include-invalid toggle ------------------
    @app.callback(
        Output("navigate", "href"),
        Input("inv-include-invalid", "value"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _toggle_include_invalid(
        values: list | None, pathname: str | None, search: str | None
    ):
        # The toggle rides the URL through the shell's refresh location:
        # the browser reloads the query string and the analysis set is
        # recomputed server-side. ``url.search`` keeps exactly one
        # dispatch owner (the workspace codec sync, jernerics-8c9).
        spec = parse_route(pathname)
        state = workspace.investigation_query(search)
        state["include_invalid"] = "invalid" in (values or [])
        return workspace.investigation_url(
            spec.object_id or "",
            spec.sub_id or "",
            view=state["view"],
            member=state["member"],
            include_invalid=state["include_invalid"],
            q=state["q"],
        )

    # -- Investigation editor ----------------------------------------------
    @app.callback(
        Output({"inv-edit-mode": ALL}, "className"),
        Output({"inv-edit-row": ALL}, "style"),
        Input({"inv-edit-mode": ALL}, "n_clicks"),
        State({"inv-edit-mode": ALL}, "id"),
        State({"inv-edit-state": ALL}, "data"),
        State({"inv-edit-row": ALL}, "id"),
        prevent_initial_call=True,
    )
    def _edit_editor_mode(
        _clicks: list | None,
        mode_ids: list | None,
        states: list | None,
        row_ids: list | None,
    ):
        # The seg narrows the view to the working picks; hiding rows is
        # presentation only — the working set lives in the state store.
        if not states or not pressed_props(dash.callback_context):
            raise PreventUpdate
        view, control = pattern_trigger(dash.callback_context)
        active = view if control == "inv-edit-mode" else "all"
        picked = set(states[0].get("picked") or ())
        classes = [
            "on" if item.get("inv-edit-mode") == active else None
            for item in mode_ids or []
        ]
        styles = [
            {"display": "none"}
            if active == "members" and item.get("inv-edit-row") not in picked
            else {}
            for item in row_ids or []
        ]
        return classes, styles

    SWEEP_ACTIONS = {
        "invalid": "invalid",
        "archive": "archive",
        "restore-validity": "restore_validity",
        "restore": "restore",
    }

    # -- Sweep page: live refresh, curation, retry-family picks ------------
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
        # Sub-views own their refresh through the same poll tick; a
        # wholesale body swap here would reset their client state.
        if sweep_views.view_from_search(search) is not None:
            raise PreventUpdate
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
        body = sweep.page_body(
            service, data, spec.object_id or "", sweep_id, time.time_ns(), picked
        )
        return body, {"digest": fresh, "cheap": cheap}

    @app.callback(
        Output("sweep-message", "children"),
        Output("sweep-page-body", "children", allow_duplicate=True),
        Output("sweep-page-facts-store", "data", allow_duplicate=True),
        Input({"sweep-action": ALL}, "n_clicks"),
        State({"sweep-action-reason": ALL}, "value"),
        State("url", "pathname"),
        State("url", "search"),
        State("view-store", "data"),
        prevent_initial_call=True,
    )
    def _curate_from_sweep_page(
        _clicks: list,
        reasons: list | None,
        pathname: str | None,
        search: str | None,
        view_doc: dict | None,
    ):
        context = dash.callback_context
        if not pressed_props(context):
            raise PreventUpdate
        token, control = pattern_trigger(context)
        action = (
            SWEEP_ACTIONS.get(token) if control == "sweep-action" and token else None
        )
        spec = parse_route(pathname)
        if action is None or spec.kind != "sweep":
            raise PreventUpdate
        sweep_id = spec.sub_id or ""
        reason = str((reasons or [""])[0] or "")
        ok, report = apply_curation(service, action, [sweep_id], reason)
        data = sweep.collect(service, sweep_id, sweep.via_from_search(search))
        if data is None:
            return workspace.action_message(ok, report), no_update, no_update
        picked = set(((view_doc or {}).get("scope") or {}).get("families") or ())
        body = sweep.page_body(
            service,
            data,
            spec.object_id or "",
            sweep_id,
            time.time_ns(),
            picked,
            sweep_views.view_from_search(search),
        )
        return (
            workspace.action_message(ok, report),
            body,
            {
                "digest": sweep.digest(data),
                "cheap": _content_digest(service.sweep_facts(sweep_id)),
            },
        )

    # -- Sweep page: retry-family picks into the session scope --------------
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

    # -- Sweep page sub-views ------------------------------------------------

    def _pattern_id(prop: str) -> dict[str, Any] | None:
        """The id dict of one triggered pattern prop, or ``None``."""
        raw = str(prop).split(".", 1)[0]
        if not raw.startswith("{"):
            return None
        try:
            resolved = json.loads(raw)
        except ValueError:
            return None
        return resolved if isinstance(resolved, dict) else None

    def _pattern_value(values: Any, target: dict[str, Any], keys: list[str]) -> Any:
        """The current value of the ALL-pattern input for ``target``:
        dash delivers one value per mounted component in tree order —
        a bare value when exactly one matches, ``{"id", "value"}`` dicts
        under some renderers."""
        key = str(next(iter(target.values()), ""))
        if not isinstance(values, list):
            return values if keys == [key] else None
        if values and all(
            isinstance(entry, dict) and "id" in entry for entry in values
        ):
            for entry in values:
                if entry.get("id") == target:
                    return entry.get("value")
            return None
        try:
            position = keys.index(key)
        except ValueError:
            return None
        return values[position] if position < len(values) else None

    def _key_store_payloads(key_stores: Any, keys: list[str]) -> dict[str, dict]:
        """Stored per-key payloads off the ALL state, normalized to the
        same positional-or-wrapped shapes dash delivers for inputs."""
        if key_stores is None:
            return {}
        if not isinstance(key_stores, list):
            return {keys[0]: key_stores} if keys else {}
        entries = [
            entry for entry in key_stores if isinstance(entry, dict) and "id" in entry
        ]
        if entries:
            return {
                str(entry["id"].get("sweep-series-key")): entry.get("value") or {}
                for entry in entries
                if isinstance(entry.get("id"), dict)
                and entry["id"].get("sweep-series-key")
            }
        normalized: dict[str, dict] = {}
        for key, payload in zip(keys, key_stores, strict=False):
            normalized[str(key)] = payload if isinstance(payload, dict) else {}
        return normalized

    def _series_options(snap: dict | None, keys: list[str]) -> list[dict[str, str]]:
        """Add-picker options: every offered key the view not showing."""
        offered = {
            entry["value"]
            for entry in (snap or {}).get("key_options") or []
            if isinstance(entry, dict) and entry.get("value")
        }
        return [{"label": key, "value": key} for key in sorted(offered - set(keys))]

    @app.callback(
        Output("sweep-series-blocks", "children"),
        Output("sweep-series-chips", "children"),
        Output("sweep-series-add", "options"),
        Output("sweep-series-add", "value"),
        Output("sweep-series-state", "data"),
        Output("sweep-series-snap", "data"),
        Output({"sweep-series-key": ALL}, "data"),
        Output({"sweep-series-fig": ALL}, "figure"),
        Output({"sweep-series-head": ALL}, "children"),
        Output({"sweep-series-stats": ALL}, "children"),
        Output("sweep-series-updated", "children"),
        Output("sweep-series-pcp", "figure"),
        Output("sweep-series-pcp-data", "data"),
        Input("poll", "n_intervals"),
        Input("sweep-series-add", "value"),
        Input({"sweep-series-drop": ALL}, "n_clicks"),
        Input({"sweep-series-display": ALL}, "value"),
        Input({"sweep-series-scale": ALL}, "value"),
        State("sweep-series-state", "data"),
        State("sweep-series-snap", "data"),
        State({"sweep-series-key": ALL}, "data"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _update_sweep_series(
        _tick: int | None,
        added: str | None,
        _drops: list,
        displays: list,
        scales: list,
        state: dict | None,
        snap: dict | None,
        key_stores: list,
        pathname: str | None,
        search: str | None,
    ):
        if sweep_views.view_from_search(search) != "series":
            raise PreventUpdate
        route = sweep_views.route_sweep(pathname)
        if route is None:
            raise PreventUpdate
        project, sweep_id = route
        context = dash.callback_context
        triggered = {str(prop) for prop in context.triggered_prop_ids}
        pressed = pressed_props(context)
        state = state or sweep_views.default_series_state()
        old_display = dict(state.get("display") or {})
        old_scale = dict(state.get("scale") or {})
        keys = list(state.get("keys") or [])
        display, scale = dict(old_display), dict(old_scale)
        keys_changed = False
        if "sweep-series-add.value" in triggered and added and added not in keys:
            keys = [*keys, str(added)]
            keys_changed = True
        for prop in pressed:
            target = _pattern_id(prop)
            if not target or "sweep-series-drop" not in target:
                continue
            dropped = str(target["sweep-series-drop"])
            keys = [entry for entry in keys if entry != dropped]
            keys_changed = True
        for prop in triggered:
            target = _pattern_id(prop)
            if not target:
                continue
            if "sweep-series-display" in target:
                value = _pattern_value(displays, target, keys)
                if value is not None:
                    display[str(target["sweep-series-display"])] = value
            if "sweep-series-scale" in target:
                value = _pattern_value(scales, target, keys)
                if value is not None:
                    scale[str(target["sweep-series-scale"])] = value
        controls_keys = {
            key
            for key in keys
            if display.get(key) != old_display.get(key)
            or scale.get(key) != old_scale.get(key)
        }
        facts = sweep_views.facts_digest(service, sweep_id)
        digests = sweep_views.series_key_digests(service, project, sweep_id)
        old_digests = state.get("digests") or {}
        stale = [key for key in keys if digests.get(key) != old_digests.get(key)]
        facts_changed = facts != state.get("cheap")
        if not keys_changed and not controls_keys and not stale and not facts_changed:
            raise PreventUpdate
        now = time.time_ns()
        stored = _key_store_payloads(key_stores, keys)
        if keys_changed or facts_changed or snap is None:
            snapshot = sweep_views.series_snapshot_fetch(
                service, project, sweep_id, keys, now
            )
            payloads, snap = sweep_views.series_split(snapshot, keys)
        else:
            payloads = dict(stored)
            if stale:
                payloads.update(
                    sweep_views.series_keys_refetch(
                        service,
                        project,
                        sweep_id,
                        stale,
                        (snap or {}).get("trials") or [],
                        now,
                    )
                )
        data_keys = {key for key in keys if payloads.get(key) != stored.get(key)}
        state = {
            "keys": keys,
            "display": display,
            "scale": scale,
            "cheap": facts,
            "digests": digests,
        }
        if keys_changed:
            figure, pcp_data = sweep_views.series_pcp_outputs(
                state, payloads, (snap or {}).get("trials") or []
            )
            return (
                sweep_views.series_blocks(state, payloads),
                sweep_views.series_chip_spans(state),
                _series_options(snap, keys),
                None,
                state,
                snap,
                no_update,
                no_update,
                no_update,
                no_update,
                analysis.updated_ago(now),
                figure,
                pcp_data,
            )
        refreshed = bool(data_keys or facts_changed)
        if refreshed:
            figure, pcp_data = sweep_views.series_pcp_outputs(
                state, payloads, (snap or {}).get("trials") or []
            )
        else:
            figure, pcp_data = no_update, no_update

        def series_payload(key: str) -> dict:
            return payloads.get(key) or {"series": [], "stats": []}

        return (
            no_update,
            no_update,
            _series_options(snap, keys) if facts_changed else no_update,
            None,
            state,
            snap if facts_changed else no_update,
            [series_payload(key) if key in data_keys else no_update for key in keys],
            [
                sweep_views.series_key_figure(key, series_payload(key)["series"], state)
                if key in data_keys | controls_keys
                else no_update
                for key in keys
            ],
            [
                sweep_views.series_key_head(key, series_payload(key)["series"])
                if key in data_keys
                else no_update
                for key in keys
            ],
            [
                sweep_views.series_key_stats(key, series_payload(key)["stats"])
                if key in data_keys
                else no_update
                for key in keys
            ],
            analysis.updated_ago(now) if refreshed else no_update,
            figure,
            pcp_data,
        )

    @app.callback(
        Output("sweep-series-sel", "data", allow_duplicate=True),
        Input({"sweep-series-row": ALL}, "n_clicks"),
        State("sweep-series-sel", "data"),
        prevent_initial_call=True,
    )
    def _toggle_sweep_series_row(_rows: list, current: dict | None):
        pressed = pressed_props(dash.callback_context)
        picked = {str(tk) for tk in (current or {}).get("tks") or []}
        changed = False
        for prop in pressed:
            target = _pattern_id(prop)
            compound = target and target.get("sweep-series-row")
            if not compound:
                continue
            tk = str(compound).rsplit(":", 1)[-1]
            picked.symmetric_difference_update({tk})
            changed = True
        if not changed:
            raise PreventUpdate
        return {"tks": sorted(picked)}

    @app.callback(
        Output("sweep-series-pcp-note", "children"),
        Output("sweep-series-clear", "style"),
        Input("sweep-series-sel", "data"),
        State("sweep-series-pcp-data", "data"),
        prevent_initial_call=True,
    )
    def _note_sweep_series_selection(
        selection: dict | None,
        pcp_data: dict | None,
    ):
        picked = sorted(str(tk) for tk in (selection or {}).get("tks") or [])
        total = len((pcp_data or {}).get("tks") or [])
        note = f"{len(picked)} of {total} trials selected" if picked else ""
        return note, {} if picked else {"display": "none"}

    @app.callback(
        Output("sweep-series-sel", "data", allow_duplicate=True),
        Input("sweep-series-clear", "n_clicks"),
        prevent_initial_call=True,
    )
    def _clear_sweep_series_selection(_clicks: int | None):
        if not pressed_props(dash.callback_context):
            raise PreventUpdate
        return {"tks": []}

    app.clientside_callback(
        """
        function(restyle, data) {
            const no = window.dash_clientside.no_update;
            if (!restyle || !data || !data.tks || !data.tks.length) {
                return no;
            }
            if (!JSON.stringify(restyle).includes("constraintrange")) {
                return no;
            }
            const host = document.getElementById("sweep-series-pcp");
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
        Output("sweep-series-sel", "data", allow_duplicate=True),
        Input("sweep-series-pcp", "restyleData"),
        State("sweep-series-pcp-data", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(selection, data) {
            const no = window.dash_clientside.no_update;
            const host = document.getElementById("sweep-series-pcp");
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
        Output("sweep-series-echo", "data"),
        Input("sweep-series-sel", "data"),
        State("sweep-series-pcp-data", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(selection, state) {
            const no = window.dash_clientside.no_update;
            const tks = new Set((selection && selection.tks) || []);
            document.querySelectorAll('#sweep-series-blocks tr.trial-row')
                .forEach((row) => {
                    let tk = null;
                    try {
                        tk = JSON.parse(row.id)["sweep-series-row"]
                            .split(":").pop();
                    } catch (err) {
                        return;
                    }
                    row.classList.toggle('row-sel', tks.has(tk));
                    row.classList.toggle('row-dim', tks.size > 0 && !tks.has(tk));
                });
            document.querySelectorAll('#sweep-series-blocks .js-plotly-plot')
                .forEach((gd) => {
                    if (!window.Plotly || !gd.data) return;
                    const opacity = gd.data.map((trace) => {
                        if (!trace.customdata || !trace.customdata.length) {
                            return trace.opacity;
                        }
                        return tks.has(String(trace.customdata[0])) || !tks.size
                            ? 1 : 0.15;
                    });
                    Plotly.restyle(gd, {opacity: opacity});
                });
            return no;
        }
        """,
        Output("sweep-series-echo", "data", allow_duplicate=True),
        Input("sweep-series-sel", "data"),
        Input("sweep-series-state", "data"),
        prevent_initial_call=True,
    )

    # -- Points: the sweep-scoped selection split --------------------------

    @app.callback(
        Output("sweep-points-grid", "rowData"),
        Output("sweep-points-note", "children"),
        Output("sweep-points-clear", "style"),
        Input("sweep-points-sel", "data"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _filter_sweep_points_rows(
        selection: dict | None,
        pathname: str | None,
        search: str | None,
    ):
        if sweep_views.view_from_search(search) != "points":
            raise PreventUpdate
        route = sweep_views.route_sweep(pathname)
        if route is None:
            raise PreventUpdate
        project, sweep_id = route
        built = sweep_views.points_view(service, project, sweep_id)
        picked = sorted(str(tk) for tk in (selection or {}).get("tks") or [])
        rows = built["view"]["rows"]
        if picked:
            keep = set(picked)
            rows = [row for row in rows if row["tk"] in keep]
        note = (
            f"{len(rows)} of {len(built['view']['rows'])} trials shown"
            if picked
            else ""
        )
        return (
            rows,
            note,
            {} if picked else {"display": "none"},
        )

    app.clientside_callback(
        """
        function(restyle, data) {
            const no = window.dash_clientside.no_update;
            if (!restyle || !data || !data.tks || !data.tks.length) {
                return no;
            }
            if (!JSON.stringify(restyle).includes("constraintrange")) {
                return no;
            }
            const host = document.getElementById("sweep-points-figure");
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
        Output("sweep-points-sel", "data"),
        Input("sweep-points-figure", "restyleData"),
        State("sweep-points-data", "data"),
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
        Output("sweep-points-sel", "data", allow_duplicate=True),
        Input("sweep-points-grid", "selectedRows"),
        State("sweep-points-data", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """
        function(selection, data) {
            const no = window.dash_clientside.no_update;
            const host = document.getElementById("sweep-points-figure");
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
        Output("sweep-points-echo", "data"),
        Input("sweep-points-sel", "data"),
        State("sweep-points-data", "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("sweep-points-sel", "data", allow_duplicate=True),
        Output("sweep-points-grid", "selectedRows"),
        Input("sweep-points-clear", "n_clicks"),
        prevent_initial_call=True,
    )
    def _clear_sweep_points_selection(_clicks: int | None):
        if not pressed_props(dash.callback_context):
            raise PreventUpdate
        return {"tks": []}, []

    @app.callback(
        Output("sweep-points-figure", "figure"),
        Input("sweep-points-params", "value"),
        State("sweep-points-params", "options"),
        State("sweep-points-data", "data"),
        prevent_initial_call=True,
    )
    def _pick_sweep_points_params(
        picked: list[str] | None,
        options: list[dict[str, str]] | None,
        data: dict | None,
    ):
        dims = (data or {}).get("dims") or []
        offered = {entry["value"] for entry in options or []}
        keep = None
        if picked:
            keep = {str(key) for key in picked} | {
                dim["label"] for dim in dims if dim["label"] not in offered
            }
        return figures.points_parcoords(dims, keep=keep)

    # -- Search: the trial filter ------------------------------------------

    @app.callback(
        Output("sweep-search-results", "children"),
        Output("sweep-search-note", "children"),
        Output("sweep-search-data", "data"),
        Input("sweep-search-q", "value"),
        Input("poll", "n_intervals"),
        State("sweep-search-data", "data"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _update_sweep_search(
        query: str | None,
        _tick: int | None,
        data: dict | None,
        pathname: str | None,
        search: str | None,
    ):
        if sweep_views.view_from_search(search) != "search":
            raise PreventUpdate
        route = sweep_views.route_sweep(pathname)
        if route is None:
            raise PreventUpdate
        project, sweep_id = route
        data = data or {}
        if "poll.n_intervals" in {
            str(prop) for prop in dash.callback_context.triggered_prop_ids
        }:
            cheap = sweep_views.facts_digest(service, sweep_id)
            if cheap != data.get("cheap"):
                data = sweep_views.search_data_fetch(
                    service, project, sweep_id, time.time_ns()
                )
        needle = str(query or "").strip().casefold()
        rows = sweep_views.search_rows(data, needle)
        total = len(data.get("rows") or [])
        return (
            page.scroll_table(
                [
                    page.head_cell("Trial", numeric=True),
                    page.head_cell("State"),
                    page.head_cell("Objective", numeric=True),
                    page.head_cell("Config"),
                ],
                rows,
                sortable=True,
            ),
            f"{len(rows)} of {total} trials",
            data,
        )

    # -- Optuna: mirrored-state figures -------------------------------------

    @app.callback(
        Output("sweep-optuna-data", "data"),
        Output("sweep-optuna-history", "figure"),
        Output("sweep-optuna-parcoords", "figure"),
        Output("sweep-optuna-slices", "figure"),
        Output("sweep-optuna-contour", "figure"),
        Output("sweep-optuna-timeline", "figure"),
        Output("sweep-optuna-x", "options"),
        Output("sweep-optuna-y", "options"),
        Input("poll", "n_intervals"),
        State("sweep-optuna-data", "data"),
        State("sweep-optuna-x", "value"),
        State("sweep-optuna-y", "value"),
        State("url", "pathname"),
        State("url", "search"),
        prevent_initial_call=True,
    )
    def _refresh_sweep_optuna(
        _tick: int | None,
        data: dict | None,
        x_key: str | None,
        y_key: str | None,
        pathname: str | None,
        search: str | None,
    ):
        if sweep_views.view_from_search(search) != "optuna":
            raise PreventUpdate
        route = sweep_views.route_sweep(pathname)
        if route is None:
            raise PreventUpdate
        project, sweep_id = route
        cheap = sweep_views.facts_digest(service, sweep_id)
        if cheap == (data or {}).get("cheap"):
            raise PreventUpdate
        trials = service.analysis_trials(project, sweep_views.sweep_tray(sweep_id))
        options = [
            {"label": key, "value": key}
            for key in sweep_views.numeric_param_keys_for(trials)
        ]
        return (
            {"trials": trials, "cheap": cheap},
            figures.optimization_history(trials),
            figures.parallel_coordinates(trials),
            figures.slice_figure(trials),
            sweep_views.contour_figure(trials, x_key, y_key),
            figures.trial_timeline(trials),
            options,
            options,
        )

    @app.callback(
        Output("sweep-optuna-contour", "figure", allow_duplicate=True),
        Input("sweep-optuna-x", "value"),
        Input("sweep-optuna-y", "value"),
        State("sweep-optuna-data", "data"),
        prevent_initial_call=True,
    )
    def _reaxis_sweep_optuna_contour(
        x_key: str | None,
        y_key: str | None,
        data: dict | None,
    ):
        if not pressed_props(dash.callback_context):
            raise PreventUpdate
        trials = (data or {}).get("trials") or []
        return sweep_views.contour_figure(trials, x_key, y_key)
