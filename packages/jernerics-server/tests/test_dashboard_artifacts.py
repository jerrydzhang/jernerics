"""Artifact and stored-log dashboard views (jernerics-h5d.14).

Callback-layer coverage over one seeded trial: version listings on the
trial and execution pages, the dispatched viewer renderers, bounded
text reads, and the session-protected raw download alias. The
orchestrator browser-drives the mounted dashboard after merge, so these
tests assert on the pure helpers the Dash callbacks wrap plus
TestClient route facts.
"""

import base64
import hashlib
import json
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, quote, unquote

import pytest
from dash import dcc, html
from dash.development.base_component import Component
from dash_ag_grid import AgGrid
from fastapi.testclient import TestClient
from jernerics_schema import (
    PROTOCOL_VERSION,
    ArtifactDeclarationEvent,
    ExecutionStartEvent,
    FlatContext,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
)
from jernerics_server.dashboard import artifacts
from jernerics_server.dashboard.artifacts import (
    TEXT_CAP,
    _json_tree,
    raw_href,
    renderer_name,
    viewer_href,
)
from jernerics_server.dashboard.callbacks import page_content
from jernerics_server.dashboard.components import short_id
from jernerics_server.dashboard.routes import parse_route
from jernerics_server.http import create_app
from jernerics_server.store import Store

API_KEY = "secret123"
PROJECT = "lab"

SWEEP = uuid.UUID("aa510000-0000-4000-8000-000000000000")
TRIAL = uuid.UUID("cc510000-0000-4000-8000-000000000000")
EXECUTION = uuid.UUID("dd510000-0000-4000-8000-000000000000")

MODEL_V1 = uuid.UUID("ee510000-0000-4000-8000-000000000000")
MODEL_V2 = uuid.UUID("ee510100-0000-4000-8000-000000000000")
INSPECTION = uuid.UUID("ee510200-0000-4000-8000-000000000000")
BIGLOG = uuid.UUID("ee510300-0000-4000-8000-000000000000")
PNG = uuid.UUID("ee510400-0000-4000-8000-000000000000")
PENDING = uuid.UUID("ee510500-0000-4000-8000-000000000000")
CUSTOM = uuid.UUID("ee510600-0000-4000-8000-000000000000")
STDOUT = uuid.UUID("ee510700-0000-4000-8000-000000000000")
STDERR = uuid.UUID("ee510800-0000-4000-8000-000000000000")

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\x0dIDATx\x9cc\xfc"
    b"\xcf\xc0\xf0\x1f\x00\x05\x05\x02\x00Z\xdb\x8d\xb4\x00\x00\x00\x00"
    b"IEND\xaeB`\x82"
)
"""A real 1x1 RGBA PNG (the browser renderer gets actual PNG bytes)."""

INSPECTION_ROWS = [
    {"step": i, "loss": round(1.0 / (i + 1), 6), "split": "train"} for i in range(200)
]
INSPECTION_BYTES = json.dumps(
    {"summary": {"accuracy": 0.91, "n_rows": 200}, "rows": INSPECTION_ROWS}
).encode()
BIGLOG_BYTES = b"0123456789abcdef" * 20_000
"""320_000 bytes of ASCII — past the 256 KiB text cap."""

SPEC: list[tuple[int, uuid.UUID, str, str, str, bytes | None, str]] = [
    (
        60,
        MODEL_V1,
        "model",
        "model.bin",
        "application/octet-stream",
        b"model-one-bytes",
        "user",
    ),
    (
        50,
        MODEL_V2,
        "model",
        "model-v2.bin",
        "application/octet-stream",
        b"model-two-bytes",
        "user",
    ),
    (
        40,
        INSPECTION,
        "inspection.json",
        "inspection.json",
        "application/json",
        INSPECTION_BYTES,
        "user",
    ),
    (30, BIGLOG, "big", "big.log", "text/plain", BIGLOG_BYTES, "user"),
    (25, PNG, "plot", "plot.png", "image/png", PNG_BYTES, "user"),
    (
        20,
        PENDING,
        "pending.bin",
        "pending.bin",
        "application/octet-stream",
        None,
        "user",
    ),
    (
        15,
        CUSTOM,
        "custom",
        "custom.bin",
        "application/x-custom",
        b"custom-payload",
        "user",
    ),
    (
        10,
        STDOUT,
        "stdout",
        "trial-0.stdout",
        "text/plain",
        b"stdout line 1\nstdout line 2\n",
        "system",
    ),
    (5, STDERR, "stderr", "trial-0.stderr", "text/plain", b"stderr line 1\n", "system"),
]


def _seed_events() -> list:
    """One completed trial with one open execution and the artifact set:
    two versions of key ``model``, an inspection.json ({summary, rows}),
    a >256 KiB text log, a real PNG, a declared-but-never-uploaded blob,
    an unknown MIME, and the system stdout/stderr pair."""
    now = datetime.now(UTC)

    def at(seconds_ago: float) -> datetime:
        return now - timedelta(seconds=seconds_ago)

    def event(cls, seconds_ago: float, **kwargs):
        return cls(event_id=uuid.uuid4(), recorded_at=at(seconds_ago), **kwargs)

    events: list = [
        event(
            SweepSnapshotEvent,
            100,
            project=PROJECT,
            sweep_id=SWEEP,
            name="alpha",
            state="completed",
        ),
        event(
            TrialSnapshotEvent,
            90,
            trial_id=TRIAL,
            sweep_id=SWEEP,
            number=0,
            state=TrialState.COMPLETED,
            retry_root_trial_id=TRIAL,
        ),
        event(
            ExecutionStartEvent,
            80,
            execution_id=EXECUTION,
            trial_id=TRIAL,
            hostname="node01",
            started_at=at(80),
        ),
    ]
    for (
        seconds_ago,
        artifact_id,
        key,
        filename,
        content_type,
        payload,
        source,
    ) in SPEC:
        kwargs: dict[str, Any] = {
            "artifact_id": artifact_id,
            "trial_id": TRIAL,
            "execution_id": EXECUTION,
            "key": key,
            "filename": filename,
            "content_type": content_type,
            "size_bytes": len(payload) if payload is not None else 8,
            "source": source,
        }
        if payload is not None:
            kwargs["sha256"] = hashlib.sha256(payload).hexdigest()
        if key == "inspection.json":
            kwargs["context"] = FlatContext({"stage": "eval"})
        events.append(event(ArtifactDeclarationEvent, seconds_ago, **kwargs))
    return events


def _walk(component: Component):
    yield component
    children = getattr(component, "children", None)
    if isinstance(children, Component):
        yield from _walk(children)
    elif isinstance(children, list | tuple):
        for child in children:
            if isinstance(child, Component):
                yield from _walk(child)


def _find(page: Any, cls: type, comp_id: str | None = None) -> list:
    return [
        component
        for component in _walk(page)
        if isinstance(component, cls) and (comp_id is None or component.id == comp_id)
    ]


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "artifacts.sqlite")
    root = tmp_path / "blobs"
    app = create_app(
        store,
        api_key=API_KEY,
        artifacts_root=root,
        dashboard=True,
    )
    client = TestClient(app, base_url="https://testserver")
    bearer = {"Authorization": f"Bearer {API_KEY}"}
    response = client.post(
        "/ingest",
        json={
            "protocol_version": PROTOCOL_VERSION,
            "events": [event.model_dump(mode="json") for event in _seed_events()],
        },
        headers=bearer,
    )
    assert response.status_code == 200, response.text
    for _, artifact_id, _, _, _, payload, _ in SPEC:
        if payload is None:
            continue
        put = client.put(
            f"/artifact/{artifact_id.hex}", content=payload, headers=bearer
        )
        assert put.status_code == 200, (artifact_id, put.text)
    login = client.post(
        "/dashboard/login", data={"api_key": API_KEY}, follow_redirects=False
    )
    assert login.status_code == 303
    return SimpleNamespace(app=app, client=client, service=app.state.dashboard.service)


class TestVersionList:
    def test_repeated_key_keeps_two_version_rows_by_declared_time(self, env):
        rows = [
            row for row in env.service.trial_artifacts(str(TRIAL)) if row.key == "model"
        ]
        assert [row.version for row in rows] == [1, 2]
        assert [row.artifact_id for row in rows] == [str(MODEL_V1), str(MODEL_V2)]
        assert all(row.available for row in rows)
        assert rows[0].sha256 != rows[1].sha256


class TestCellTextSelection:
    """jernerics-eqn: the listing and rows grids carry
    enableCellTextSelection + ensureDomOrder so identifiers (ids,
    sha256) stay copyable, without dropping existing options."""

    def test_rows_grid_keeps_quick_filter_and_carries_the_pair(self, env):
        page, _ = page_content(
            f"/dashboard/artifact-view/{INSPECTION.hex}", env.service
        )
        options = _find(page, AgGrid, "artifact-rows-grid")[0].dashGridOptions
        assert options == {
            "enableCellTextSelection": True,
            "ensureDomOrder": True,
            "pagination": False,
            "quickFilterText": "",
        }

    def test_quick_filter_rewrite_keeps_the_pair(self, env):
        """The filter callback replaces dashGridOptions wholesale; going
        through grid_options keeps cells selectable after filtering."""
        response = env.client.post(
            "/dashboard/_dash-update-component",
            json={
                "output": "artifact-rows-grid.dashGridOptions",
                "outputs": {
                    "id": "artifact-rows-grid",
                    "property": "dashGridOptions",
                },
                "inputs": [
                    {
                        "id": "artifact-quick-filter",
                        "property": "value",
                        "value": "train",
                    }
                ],
                "changedPropIds": ["artifact-quick-filter.value"],
            },
        )
        assert response.status_code == 200, response.text
        options = response.json()["response"]["artifact-rows-grid"]["dashGridOptions"]
        assert options == {
            "enableCellTextSelection": True,
            "ensureDomOrder": True,
            "pagination": False,
            "quickFilterText": "train",
        }


class TestPendingState:
    def test_declared_only_row_is_pending_and_viewer_is_factual(self, env):
        rows = env.service.trial_artifacts(str(TRIAL))
        pending = next(row for row in rows if row.key == "pending.bin")
        assert pending.available is False

        page, polls = page_content(
            f"/dashboard/artifact-view/{PENDING.hex}", env.service
        )
        rendered = str(page)
        assert "blob not received" in rendered
        assert "pending.bin" in rendered
        assert "pending" in rendered
        assert polls is False

    def test_pending_download_404s_and_text_read_returns_none(self, env):
        response = env.client.get(f"/dashboard/artifact/{PENDING.hex}")
        assert response.status_code == 404
        assert env.service.read_artifact_text(str(PENDING), TEXT_CAP) is None

    def test_unknown_artifact_id_renders_missing_page(self, env):
        page, polls = page_content(
            "/dashboard/artifact-view/0123456789abcdef0123456789abcdef", env.service
        )
        assert "Nothing here yet" in str(page)
        assert polls is False


class TestJsonRenderers:
    def test_inspection_shape_renders_summary_chips_and_200_row_grid(self, env):
        page, _ = page_content(
            f"/dashboard/artifact-view/{INSPECTION.hex}", env.service
        )
        grid = _find(page, AgGrid, "artifact-rows-grid")[0]
        assert len(grid.rowData) == 200
        assert [column["field"] for column in grid.columnDefs] == [
            "step",
            "loss",
            "split",
        ]
        rendered = str(page)
        assert "accuracy: 0.91" in rendered
        assert "n_rows: 200" in rendered
        chips = [span for span in _find(page, html.Span) if span.className == "chip"]
        assert any("accuracy: 0.91" in str(chip.children) for chip in chips)
        assert _find(page, dcc.Input, "artifact-quick-filter")

    def test_generic_json_object_renders_collapsible_tree(self, env):
        tree = _json_tree({"config": {"lr": 0.1}, "tags": ["a", "b"]})
        rendered = str(tree)
        assert "lr: 0.1" in rendered
        assert "tags · list[2]" in rendered
        assert "Details" in rendered


class TestTextRenderer:
    def test_large_log_shows_first_256kib_and_truncation_notice(self, env):
        read = env.service.read_artifact_text(str(BIGLOG), TEXT_CAP)
        assert read is not None
        text, truncated = read
        assert truncated is True
        assert text == BIGLOG_BYTES[:TEXT_CAP].decode()

        page, _ = page_content(f"/dashboard/artifact-view/{BIGLOG.hex}", env.service)
        rendered = str(page)
        assert "truncated (showing first 256 KiB of 312.5 KiB)" in rendered
        pre = _find(page, html.Pre)[0]
        assert pre.children == BIGLOG_BYTES[:TEXT_CAP].decode()
        assert len(_find(page, AgGrid)) == 0

    def test_small_text_renders_without_notice(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{CUSTOM.hex}", env.service)
        assert "truncated" not in str(page)


class TestLogPresentation:
    def test_logs_and_text_render_as_mono_pre_blocks(self, env):
        for artifact_id in (STDOUT, STDERR, BIGLOG):
            page, _ = page_content(
                f"/dashboard/artifact-view/{artifact_id.hex}", env.service
            )
            pres = [pre for pre in _find(page, html.Pre) if pre.className]
            assert any(pre.className == "mono" for pre in pres)
            assert not any(pre.className == "log-view" for pre in pres)


class TestMediaRenderers:
    def test_image_renders_img_with_raw_url(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{PNG.hex}", env.service)
        images = _find(page, html.Img)
        assert len(images) == 1
        assert images[0].src == raw_href(str(PNG)) == f"/dashboard/artifact/{PNG.hex}"

    def test_audio_and_video_renders_html5_elements(self, env):
        base = env.service.artifact_view(str(CUSTOM))
        assert base is not None
        audio_view = replace(
            base,
            key="clip",
            filename="clip.mp3",
            content_type="audio/mpeg",
        )
        audio_page = artifacts.viewer_page(env.service, audio_view, 0)
        players = _find(audio_page, html.Audio)
        assert len(players) == 1
        assert players[0].src == raw_href(str(CUSTOM))

        video_view = replace(
            base,
            key="clip",
            filename="clip.mp4",
            content_type="video/mp4",
        )
        video_page = artifacts.viewer_page(env.service, video_view, 0)
        players = _find(video_page, html.Video)
        assert len(players) == 1
        assert players[0].src == raw_href(str(CUSTOM))

    def test_renderer_dispatch_table(self):
        assert renderer_name("application/json", "x", "k") == "json"
        assert renderer_name("text/plain", "x", "k") == "text"
        assert renderer_name("application/octet-stream", "x", "stdout") == "log"
        assert renderer_name("application/octet-stream", "x", "stderr") == "log"
        assert renderer_name("image/png", "plot.png", "plot") == "image"
        assert renderer_name("audio/mpeg", "clip.mp3", "clip") == "audio"
        assert renderer_name("video/mp4", "clip.mp4", "clip") == "video"
        assert (
            renderer_name("application/x-custom", "custom.bin", "custom") == "fallback"
        )
        assert (
            renderer_name("application/octet-stream", "weights.bin", "model")
            == "fallback"
        )

    def test_binary_never_enters_page_state(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{PNG.hex}", env.service)
        rendered = str(page)
        assert base64.b64encode(PNG_BYTES).decode() not in rendered
        images = _find(page, html.Img)
        assert images[0].src.startswith("/dashboard/artifact/")
        assert not images[0].src.startswith("data:")


class TestFallbackRenderer:
    def test_unknown_mime_shows_metadata_download_card(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{CUSTOM.hex}", env.service)
        rendered = str(page)
        assert "no inline renderer for application/x-custom" in rendered
        notes = [p for p in _find(page, html.P) if p.className == "sub"]
        assert any("no inline renderer" in str(note.children) for note in notes)
        links = [a for a in _find(page, html.A) if a.children == "Download"]
        assert links[0].href == raw_href(str(CUSTOM))
        assert links[0].className == "btn"


class TestSessionProtectedDownload:
    def test_authed_download_ok_unauth_redirects_range_exact(self, env):
        response = env.client.get(f"/dashboard/artifact/{BIGLOG.hex}")
        assert response.status_code == 200
        assert response.content == BIGLOG_BYTES

        stranger = TestClient(env.app, base_url="https://testserver")
        denied = stranger.get(
            f"/dashboard/artifact/{BIGLOG.hex}", follow_redirects=False
        )
        assert denied.status_code == 303
        assert denied.headers["location"] == (
            "/dashboard/login?next="
            + quote(f"/dashboard/artifact/{BIGLOG.hex}", safe="")
        )

        ranged = env.client.get(
            f"/dashboard/artifact/{BIGLOG.hex}", headers={"Range": "bytes=100-199"}
        )
        assert ranged.status_code == 206
        assert ranged.content == BIGLOG_BYTES[100:200]

    def test_viewer_route_parses(self):
        spec = parse_route(f"/dashboard/artifact-view/{PNG.hex}")
        assert spec.kind == "artifact"
        assert spec.object_id == PNG.hex
        assert parse_route("/dashboard/artifact-view/").kind == "not-found"
        assert viewer_href(str(PNG)) == f"/dashboard/artifact-view/{PNG.hex}"


class TestViewerFacts:
    def test_header_shows_facts_links_and_download(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{MODEL_V2.hex}", env.service)
        rendered = str(page)
        assert "v2 of 2" in rendered
        assert "model-v2.bin" in rendered
        assert hashlib.sha256(b"model-two-bytes").hexdigest() in rendered
        assert "/dashboard/project/lab?view=" in rendered
        assert str(TRIAL) in rendered
        assert str(EXECUTION) in rendered
        assert str(SWEEP) in rendered

    def test_facts_render_as_scroll_table_with_download_action(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{MODEL_V2.hex}", env.service)
        scroll = next(
            node
            for node in _walk(page)
            if getattr(node, "className", None) == "table-scroll"
        )
        heads = [th.children for th in _walk(scroll) if isinstance(th, html.Th)]
        assert heads == ["Fact", "Value"]
        rows = [
            tr
            for tr in _walk(scroll)
            if isinstance(tr, html.Tr) and isinstance(tr.children[0], html.Td)
        ]
        labels = [tr.children[0].children for tr in rows]
        assert "Key" in labels
        assert "SHA-256" in labels
        state_row = next(tr for tr in rows if tr.children[0].children == "State")
        badge = _find(state_row, html.Span)[0]
        assert badge.className == "badge badge-ok"
        actions = next(
            node
            for node in _walk(page)
            if getattr(node, "className", None) == "actions"
        )
        download = _find(actions, html.A)[0]
        assert download.children == "Download"
        assert download.className == "btn"
        assert download.href == raw_href(str(MODEL_V2))


class TestViewerTitleAndCrumbs:
    def test_title_is_filename_with_key_not_raw_hex(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{MODEL_V2.hex}", env.service)
        h1s = _find(page, html.H1)
        assert [h.children for h in h1s] == ["model-v2.bin"]
        headings = [str(h.children) for h in h1s + _find(page, html.H2)]
        assert all(MODEL_V2.hex not in text for text in headings)
        assert "model" in str(page)

    def test_breadcrumbs_link_workspace_sweep_trial_execution(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{CUSTOM.hex}", env.service)
        crumb = next(
            node for node in _walk(page) if getattr(node, "className", None) == "crumb"
        )
        links = [(a.children, a.href) for a in _find(crumb, html.A)]
        assert [label for label, _ in links] == [
            "lab",
            "alpha",
            short_id(str(TRIAL)),
            short_id(str(EXECUTION)),
        ]
        assert links[0][1] == "/dashboard/project/lab"
        for (_, href), kind, object_id in zip(
            links[1:],
            ("sweep", "trial", "execution"),
            (SWEEP, TRIAL, EXECUTION),
            strict=True,
        ):
            path, _, query = href.partition("?")
            assert path == "/dashboard/project/lab"
            doc = json.loads(unquote(parse_qs(query)["view"][0]))
            assert doc["focus"] == {"kind": kind, "id": str(object_id)}
        assert str(crumb.children[-1]) == "custom.bin"


class TestViewerShell:
    def test_viewer_is_bare_new_shell_page_without_legacy_chrome(self, env):
        page, _ = page_content(f"/dashboard/artifact-view/{CUSTOM.hex}", env.service)
        assert getattr(page, "className", None) == "np"
        link = _find(page, html.Link)[0]
        assert link.href == "/dashboard/assets/page.css"
        assert not any(
            getattr(node, "className", None) == "topbar" for node in _walk(page)
        )
        rendered = str(page)
        for gone in (
            "artifact-note",
            "artifact-card",
            "artifact-text",
            "crumbs",
            "quick-filter",
            "log-view",
            "text-view",
            "summary-card",
            "section-artifact",
        ):
            assert gone not in rendered


class TestNoNewSqlOutsideQueries:
    _SQL = re.compile(
        r"\bSELECT\b[\s\S]{0,400}\bFROM\b|\bINSERT INTO\b|\bUPDATE\s+\w+\s+SET\b"
    )
    _SQL_MODULES = {"queries.py", "store.py", "ingest.py"}

    def test_sql_statements_stay_in_the_sql_layer(self):
        root = Path(__file__).parent.parent / "src" / "jernerics_server"
        for path in sorted(root.rglob("*.py")):
            if path.name in self._SQL_MODULES:
                continue
            assert not self._SQL.search(path.read_text()), path
