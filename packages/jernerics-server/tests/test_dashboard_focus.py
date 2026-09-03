import uuid

from jernerics_server.dashboard.analysis import (
    auto_refresh_flip,
    axis_state_edit,
    default_view_state,
    edited_view,
    encode_view_state,
    hydrate_view,
    moved_keys,
    view_from_context_filter,
    view_from_controls,
    view_from_include,
    view_from_trace_click,
    view_query,
    with_focus,
)

SWEEP_A = uuid.UUID("aa110000-0000-4000-8000-000000000000")
TRIAL_F2 = uuid.UUID("cc130000-0000-4000-8000-000000000000")
WORKSPACE = "/dashboard/project/ops"

_SERIES_DATA = {"per_key": {"loss": {"series": [{"points": [(0, 1.0), (1, 2.0)]}]}}}


def _focused(kind: str = "sweep", object_id: uuid.UUID = SWEEP_A) -> dict:
    return with_focus(None, {"kind": kind, "id": str(object_id)})


def _control_edit(doc: dict, edited: set[str], **values) -> dict:
    blanks = {
        name: None
        for name in (
            "active",
            "keys",
            "mode",
            "reduction",
            "color",
            "facet",
            "contour_x",
            "contour_y",
        )
    }
    return view_from_controls(doc, **{**blanks, **values}, edited=edited)


class TestEditedViewFunnel:
    def test_changes_apply_and_focus_survives(self):
        doc = _focused()
        edited = edited_view(doc, {"active": "investigations"})
        assert edited["active"] == "investigations"
        assert edited["focus"] == doc["focus"]

    def test_change_that_names_focus_overrides_it(self):
        doc = _focused()
        assert edited_view(doc, {"focus": None})["focus"] is None

    def test_absent_current_starts_from_defaults(self):
        assert edited_view(None, {"active": "investigations"}) == dict(
            default_view_state(), active="investigations"
        )


class TestFocusSurvivesViewEdits:
    def test_tab_switch_keeps_focus(self):
        doc = _focused()
        switched = _control_edit(doc, {"active"}, active="investigations")
        assert switched["active"] == "investigations"
        assert switched["focus"] == doc["focus"]

    def test_series_control_edits_keep_focus(self):
        doc = _focused()
        picked = _control_edit(doc, {"keys"}, keys=["loss", "acc"])
        assert picked["series"]["keys"] == ["loss", "acc"]
        assert picked["focus"] == doc["focus"]
        recolored = _control_edit(picked, {"color"}, color="shard")
        assert recolored["series"]["color"] == "shard"
        assert recolored["focus"] == doc["focus"]

    def test_auto_refresh_flip_keeps_focus(self):
        doc = dict(_focused(), auto_refresh=True)
        assert auto_refresh_flip(doc, True) is None  # scope open: no write
        flipped = auto_refresh_flip(doc, False)
        assert flipped is not None and flipped["auto_refresh"] is False
        assert flipped["focus"] == doc["focus"]

    def test_include_edit_keeps_focus_and_picks(self):
        doc = _focused()
        doc["scope"]["sweeps"] = [str(SWEEP_A)]
        included = view_from_include(doc, ["archived", "invalid"])
        assert included["scope"]["include_archived"] is True
        assert included["scope"]["include_invalid"] is True
        assert included["scope"]["sweeps"] == [str(SWEEP_A)]
        assert included["focus"] == doc["focus"]

    def test_context_filter_edit_keeps_focus(self):
        doc = _focused()
        filtered = view_from_context_filter(doc, "host", ["node00", "node01"])
        assert filtered["series"]["context_filters"] == {"host": ["node00", "node01"]}
        assert filtered["focus"] == doc["focus"]
        cleared = view_from_context_filter(filtered, "host", [])
        assert cleared["series"]["context_filters"] == {}
        assert cleared["focus"] == doc["focus"]

    def test_axis_edits_keep_focus(self):
        doc = _control_edit(_focused(), {"keys"}, keys=["loss"])
        overlay, _note = axis_state_edit(
            doc,
            metric=None,
            control="scale",
            scale="log",
            range_mode="auto",
            low=None,
            high=None,
            data=_SERIES_DATA,
        )
        assert overlay is not None
        assert overlay["series"]["overlay_axis"]["scale"] == "log"
        assert overlay["focus"] == doc["focus"]
        per_key, _note = axis_state_edit(
            doc,
            metric="loss",
            control="scale",
            scale="log",
            range_mode="auto",
            low=None,
            high=None,
            data=_SERIES_DATA,
        )
        assert per_key is not None
        assert per_key["series"]["axes"]["loss"]["scale"] == "log"
        assert per_key["focus"] == doc["focus"]

    def test_moved_keys_keeps_focus(self):
        doc = _control_edit(_focused(), {"keys"}, keys=["loss", "acc"])
        moved = moved_keys(doc, "acc", "up")
        assert moved is not None
        assert moved["series"]["keys"] == ["acc", "loss"]
        assert moved["focus"] == doc["focus"]

    def test_trace_click_replaces_focus_and_highlights(self):
        doc = _focused()
        clicked = view_from_trace_click(
            doc, {"points": [{"customdata": str(TRIAL_F2)}]}
        )
        assert clicked is not None
        assert clicked["focus"] == {"kind": "trial", "id": str(TRIAL_F2)}
        assert clicked["highlighted_trials"] == [str(TRIAL_F2)]
        assert clicked["series"] == doc["series"]

    def test_inspector_close_clears_focus_without_narrowing_scope(self):
        doc = _control_edit(_focused(), {"keys"}, keys=["loss"])
        closed = with_focus(doc, None)
        assert closed["focus"] is None
        assert closed["series"]["keys"] == ["loss"]


class TestHydrationKeepsFocus:
    def test_no_view_param_leaves_focused_store_alone(self):
        # jernerics-gk6: the poll-tick steady state is a focused doc with
        # no view parameter to merge; hydration must not rewrite it.
        doc = _focused()
        assert hydrate_view(WORKSPACE, "", doc) == (None, None)
        assert hydrate_view(WORKSPACE, None, doc) == (None, None)

    def test_no_view_param_resets_controls_but_keeps_focus(self):
        doc = _control_edit(
            _focused(), {"keys", "active"}, keys=["loss"], active="investigations"
        )
        hydrated, error = hydrate_view(WORKSPACE, "?sel=tok", doc)
        assert error is None
        assert hydrated == _focused()

    def test_malformed_view_param_keeps_focus_and_errors(self):
        doc = _focused()
        hydrated, error = hydrate_view(WORKSPACE, "?view=%7Bbroken", doc)
        assert hydrated is not None and hydrated["focus"] == doc["focus"]
        assert error is not None and "malformed" in error

    def test_view_param_without_focus_keeps_current_focus(self):
        doc = _focused()
        shared = dict(default_view_state(), active="exceptions")
        search = f"?view={encode_view_state(shared)}"
        hydrated, error = hydrate_view(WORKSPACE, search, doc)
        assert error is None
        assert hydrated is not None
        assert hydrated["active"] == "exceptions"
        assert hydrated["focus"] == doc["focus"]

    def test_view_param_with_explicit_focus_wins(self):
        doc = _focused()
        explicit = dict(
            default_view_state(), focus={"kind": "trial", "id": str(TRIAL_F2)}
        )
        search = f"?view={encode_view_state(explicit)}"
        hydrated, error = hydrate_view(WORKSPACE, search, doc)
        assert error is None
        assert hydrated is not None
        assert hydrated["focus"] == {"kind": "trial", "id": str(TRIAL_F2)}

    def test_off_workspace_route_writes_nothing(self):
        assert hydrate_view("/dashboard/", "?view=%7Bbroken", _focused()) == (
            None,
            None,
        )

    def test_url_round_trip_is_stable_with_focus(self):
        # A focused doc mints a view parameter, and hydrating that same
        # parameter against the doc it came from is a no-op — no
        # clear/restore alternation across poll ticks.
        doc = _focused()
        fragment = view_query(doc)
        assert fragment.startswith("view=")
        assert hydrate_view(WORKSPACE, f"?{fragment}", doc) == (None, None)


class TestUrlSyncCarriesFocus:
    def test_focused_doc_mints_view_parameter(self):
        assert view_query(_focused()).startswith("view=")

    def test_default_doc_mints_no_parameter(self):
        assert view_query(default_view_state()) == ""
        assert view_query(None) == ""
