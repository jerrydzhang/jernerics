from pathlib import Path

from dash import html
from fastapi.testclient import TestClient
from jernerics_server.dashboard import page
from jernerics_server.dashboard.callbacks import page_content
from jernerics_server.dashboard.routes import parse_route
from jernerics_server.dashboard.service import DashboardService
from jernerics_server.http import create_app
from jernerics_server.queries import QueryService
from jernerics_server.store import Store

API_KEY = "secret123"


def _client(tmp_path: Path) -> TestClient:
    store = Store(tmp_path / "shell.sqlite")
    app = create_app(store, api_key=API_KEY, dashboard=True)
    client = TestClient(app, base_url="https://testserver")
    response = client.post(
        "/dashboard/login", data={"api_key": API_KEY}, follow_redirects=False
    )
    assert response.status_code == 303
    return client


def _service(tmp_path: Path) -> DashboardService:
    store = Store(tmp_path / "content.sqlite")
    return DashboardService(QueryService(store), store)


def _walk_children(node):
    yield node
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            yield from _walk_children(child)
    elif children is not None and hasattr(children, "children"):
        yield from _walk_children(children)


def _text(node) -> str:
    if isinstance(node, (list, tuple)):
        return "".join(_text(item) for item in node)
    if node is None:
        return ""
    if isinstance(node, (str, int, float)):
        return str(node)
    return _text(getattr(node, "children", None))


def _of(node, kind):
    return [item for item in _walk_children(node) if isinstance(item, kind)]


def _by_class(node, name):
    return [
        item
        for item in _walk_children(node)
        if getattr(item, "className", None) == name
    ]


class TestShellRoutes:
    def test_sweep_route_parses(self):
        spec = parse_route("/dashboard/project/ops/sweep/sw-1")
        assert (spec.kind, spec.object_id, spec.sub_id) == ("sweep", "ops", "sw-1")

    def test_exceptions_route_parses(self):
        spec = parse_route("/dashboard/project/ops/exceptions")
        assert (spec.kind, spec.object_id, spec.sub_id) == ("exceptions", "ops", None)

    def test_trailing_slashes_still_parse(self):
        swept = parse_route("/dashboard/project/ops/sweep/sw-1/")
        assert (swept.kind, swept.object_id, swept.sub_id) == ("sweep", "ops", "sw-1")
        exceptions = parse_route("/dashboard/project/ops/exceptions/")
        assert (exceptions.kind, exceptions.object_id) == ("exceptions", "ops")

    def test_unknown_project_shapes_stay_not_found(self):
        for path in (
            "/dashboard/project/ops/sweep",
            "/dashboard/project/ops/sweep//",
            "/dashboard/project/ops/sweep/sw-1/edit",
            "/dashboard/project/ops/exceptions/extra",
            "/dashboard/project/a/b/sweep/sw-1",
            "/dashboard/project/a/b/exceptions",
        ):
            assert parse_route(path).kind == "not-found", path

    def test_top_level_sweep_stays_not_found(self):
        assert parse_route("/dashboard/sweep/abc").kind == "not-found"

    def test_investigation_shapes_unchanged(self):
        shown = parse_route("/dashboard/project/ops/investigation/inv-1")
        assert (shown.kind, shown.object_id, shown.sub_id) == (
            "investigation",
            "ops",
            "inv-1",
        )

    def test_exceptions_kind_renders_nothing_yet(self, tmp_path):
        service = _service(tmp_path)
        page_html, polls = page_content("/dashboard/project/ops/exceptions", service)
        assert "Not found" in _text(page_html)
        assert polls is False

    def test_unknown_sweep_renders_missing_object(self, tmp_path):
        service = _service(tmp_path)
        page_html, polls = page_content(
            "/dashboard/project/ops/sweep/0123456789abcdef0123456789abcdef", service
        )
        text = _text(page_html)
        assert "Sweep 0123456789abcdef0123456789abcdef" in text
        assert "Nothing here yet" in text
        assert polls is False


class TestPageComposition:
    def test_page_shell_wraps_topbar_tabs_and_body(self):
        shell = page.page_shell("Exceptions", "ops", html.H1("Exceptions"))
        assert getattr(shell, "className", None) == "np"
        link = _of(shell, html.Link)[0]
        assert link.href == page.STYLESHEET_HREF
        assert _by_class(shell, "topbar")
        containers = _by_class(shell, "page")
        assert len(containers) == 1
        wide = page.page_shell("Overview", "ops", wide=True)
        assert _by_class(wide, "page-wide")

    def test_topbar_dom(self):
        bar = page.topbar("ops", scope="Sweep sw-1")
        assert getattr(bar, "className", None) == "topbar"
        brand = _by_class(bar, "brand")[0]
        assert brand.children == "jernerics"
        assert _by_class(bar, "spacer")
        assert _text(_by_class(bar, "annotate")[0]) == "Log out"
        scopebar = _by_class(bar, "scopebar")[0]
        assert _text(scopebar) == "ops·Scope: Sweep sw-1"
        assert _of(scopebar, html.B)[0].children == "Sweep sw-1"

    def test_tab_bar_active_state(self):
        bar = page.tab_bar("Exceptions", "ops")
        links = _of(bar, html.A)
        assert [link.children for link in links] == list(page.TABS)
        on = [link for link in links if link.className == "on"]
        assert [link.children for link in on] == ["Exceptions"]
        assert on[0].href == "/dashboard/project/ops/exceptions"

    def test_tiles_and_tone(self):
        row = page.tiles(
            page.tile(3, "failed executions", tone="crit", href="?f=failed"),
            page.tile(0, "completed sweeps"),
        )
        crit = _by_class(row, "tile crit")[0]
        assert crit.href == "?f=failed"
        plain = _by_class(row, "tile")[0]
        assert _text(plain) == "0completed sweeps"

    def test_scope_segment(self):
        seg = page.segment(
            [("Active (2)", "/a", True), ("All (5)", "/b", False), ("n", None, False)]
        )
        items = _of(seg, (html.A, html.Span))
        assert [item.children for item in items] == ["Active (2)", "All (5)", "n"]
        assert items[0].className == "on"
        assert items[1].className is None
        assert isinstance(items[2], html.Span)

    def test_limit_segment(self):
        seg = page.limit_segment("50")
        spans = _of(seg, html.Span)
        assert [span.children for span in spans] == ["25", "50", "all"]
        assert [span.to_plotly_json()["props"]["data-limit"] for span in spans] == [
            "25",
            "50",
            "all",
        ]
        assert [span.className for span in spans] == [None, "on", None]

    def test_scroll_table_dom(self):
        table = page.scroll_table(
            [page.head_cell("Sweep"), page.head_cell("Trials", numeric=True)],
            [html.Tr(html.Td("x"))],
            sortable=True,
        )
        assert getattr(table, "className", None) == "table-scroll"
        section = _by_class(table, "section")[0]
        dom_table = _of(section, html.Table)[0]
        assert dom_table.className == "sortable"
        heads = _of(dom_table, html.Th)
        assert [th.children for th in heads] == ["Sweep", "Trials"]
        assert heads[1].className == "num"
        assert _text(_of(dom_table, html.Td)[0]) == "x"

    def test_head_cell_sort_dir(self):
        props = page.head_cell("Trials", numeric=True, sort_dir="asc")
        assert props.to_plotly_json()["props"]["data-dir"] == "asc"

    def test_status_dot(self):
        dot = page.status_dot("stale", "3m ago")
        assert getattr(dot, "className", None) == "st st-stale"
        assert _text(dot) == "stale3m ago"
        assert _text(_by_class(dot, "note")[0]) == "3m ago"
        assert _text(page.status_dot("running")) == "running"

    def test_artifact_chips(self):
        assert page.artifact_chips([]) == ["—"]
        chips = page.artifact_chips(
            [("metrics", "abcd-1234", "metrics.json"), ("plot", "ef01", "p.png")]
        )
        first, sep, second = chips
        assert sep == " · "
        assert getattr(first, "className", None) == "art"
        assert getattr(first, "href", None) == "/dashboard/artifact-view/abcd1234"
        assert getattr(first, "title", None) == "metrics.json"
        assert getattr(second, "href", None) == "/dashboard/artifact-view/ef01"

    def test_filter_chip(self):
        chip = page.filter_chip("3 sweeps interrupted")
        assert getattr(chip, "className", None) == "chip"
        remove = _of(chip, html.A)[0]
        assert _text(chip) == "3 sweeps interrupted×"
        assert remove.children == "×"

    def test_breadcrumbs(self):
        crumbs = page.breadcrumbs(
            [
                ("ops", "/dashboard/project/ops"),
                ("Investigations", "/dashboard/project/ops/investigations"),
                "roberts",
            ]
        )
        assert getattr(crumbs, "className", None) == "crumb"
        assert [link.href for link in _of(crumbs, html.A)] == [
            "/dashboard/project/ops",
            "/dashboard/project/ops/investigations",
        ]
        dims = _by_class(crumbs, "dim")
        assert [dim.children for dim in dims] == ["›", "›"]
        assert _text(crumbs) == "ops›Investigations›roberts"

    def test_inv_nav(self):
        nav = page.inv_nav(
            "Series",
            [("Compare", "/compare"), ("Series", "/series")],
            python_href="/python",
            edit_href="/edit",
        )
        assert getattr(nav, "className", None) == "limit-row"
        assert _text(_by_class(nav, "annotate")[0]) == "Investigation views"
        seg = _by_class(nav, "seg")[0]
        assert seg.id == "inv-tabs"
        assert [link.className for link in _of(seg, html.A)] == [None, "on"]
        assert _by_class(nav, "spacer")
        buttons = [a for a in _of(nav, html.A) if a.className == "btn"]
        assert [button.children for button in buttons] == [
            "Open in Python",
            "Edit members",
        ]

    def test_pager(self):
        assert not list(_of(page.pager(1, 1), html.Button))
        buttons = _of(page.pager(2, 3), html.Button)
        assert [button.children for button in buttons] == ["‹", "1", "2", "3", "›"]
        assert [getattr(button, "className", None) for button in buttons] == [
            None,
            None,
            "on",
            None,
            None,
        ]
        assert [getattr(button, "disabled", False) for button in buttons] == [
            False,
            False,
            False,
            False,
            False,
        ]
        edges = _of(page.pager(1, 2), html.Button)
        assert edges[0].disabled is True
        assert edges[-1].disabled is False


class TestStylesheetServing:
    def test_stylesheet_is_served(self, tmp_path):
        response = _client(tmp_path).get("/dashboard/assets/page.css")
        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]
        assert ".np .topbar" in response.text
        assert "--accent: #2563eb" in response.text

    def test_stylesheet_is_not_injected_into_legacy_pages(self, tmp_path):
        response = _client(tmp_path).get("/dashboard/")
        assert response.status_code == 200
        assert "page.css" not in response.text
        assert "dashboard.css" in response.text

    def test_deep_links_serve_the_shell(self, tmp_path):
        client = _client(tmp_path)
        for path in (
            "/dashboard/project/ops/sweep/sw-1",
            "/dashboard/project/ops/exceptions",
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
