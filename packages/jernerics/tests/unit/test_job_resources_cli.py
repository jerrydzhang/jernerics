import json
from pathlib import Path
from unittest.mock import patch

import pytest
from jernerics.backend.adapter import JobResourceSnapshot, JobResourcesResult
from jernerics.cli import app
from jernerics.tracking.jsonl_io import scan_events
from jernerics_schema import JobResourceEvent
from typer.testing import CliRunner

SNAPSHOT = JobResourceSnapshot(
    job_id="990001",
    state="COMPLETED",
    exit_code="0:0",
    wall_time_s=3_723.0,
    cpu_time_s=101_400.0,
    cpu_pct=4213.45,
    max_rss_mb=2_560.0,
    ave_rss_mb=2_457.6,
    alloc_cpus=8,
    req_mem="16G",
    alloc_tres="cpu=8,mem=16G,billing=8",
    node_list="node[01-02]",
)


def test_prints_aligned_parsed_fields():
    with patch(
        "jernerics.commands.jobs.fetch_job_resources",
        return_value=JobResourcesResult([SNAPSHOT], None),
    ):
        result = CliRunner().invoke(app, ["job", "resources", "990001"])

    assert result.exit_code == 0
    assert "job_id       990001" in result.output
    assert "state        COMPLETED" in result.output
    assert "wall_time_s  3723.0" in result.output
    assert "req_mem      16G" in result.output
    assert "alloc_tres   cpu=8,mem=16G,billing=8" in result.output


def test_missing_accounting_data_is_not_an_error():
    with patch(
        "jernerics.commands.jobs.fetch_job_resources",
        return_value=JobResourcesResult([], "sacct returned no accounting row"),
    ):
        result = CliRunner().invoke(app, ["job", "resources", "990001"])

    assert result.exit_code == 0
    assert "no accounting row" in result.output


def test_missing_job_id_argument_is_a_usage_error():
    result = CliRunner().invoke(app, ["job", "resources"])

    assert result.exit_code != 0


def test_ship_without_configured_server_notes_and_succeeds(monkeypatch):
    monkeypatch.delenv("JERNERICS_TRACKING_SERVER", raising=False)
    with patch(
        "jernerics.commands.jobs.fetch_job_resources",
        return_value=JobResourcesResult([SNAPSHOT], None),
    ):
        result = CliRunner().invoke(app, ["job", "resources", "990001", "--ship"])

    assert result.exit_code == 0
    assert "No tracking server configured" in result.output


def test_ship_appends_record_with_study_from_job_meta(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "http://localhost:8000")
    monkeypatch.setattr("jernerics.commands.jobs.cache_dir", lambda: tmp_path)
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "990001.json").write_text(
        json.dumps({"job_id": "990001", "study_name": "mystudy"})
    )
    with (
        patch(
            "jernerics.commands.jobs.fetch_job_resources",
            return_value=JobResourcesResult([SNAPSHOT], None),
        ),
        patch("jernerics.commands.jobs.ship_events_file") as ship,
    ):
        result = CliRunner().invoke(app, ["job", "resources", "990001", "--ship"])

    assert result.exit_code == 0
    assert "shipped" in result.output
    ship.assert_called_once()
    path = ship.call_args.args[0]
    assert path.parent == tmp_path / "tracking" / "mystudy" / "submission"
    events, _ = scan_events(path, 0)
    event = events[0][0]
    assert isinstance(event, JobResourceEvent)
    assert event.job_id == "990001"
    assert event.study_name == "mystudy"
    assert event.submission_id is None
    assert event.wall_time_s == pytest.approx(3_723.0)


def test_ship_without_study_uses_scratch_file(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "http://localhost:8000")
    monkeypatch.setattr("jernerics.commands.jobs.cache_dir", lambda: tmp_path)
    with (
        patch(
            "jernerics.commands.jobs.fetch_job_resources",
            return_value=JobResourcesResult([SNAPSHOT], None),
        ),
        patch("jernerics.commands.jobs.ship_events_file", return_value=False) as ship,
    ):
        result = CliRunner().invoke(app, ["job", "resources", "990001", "--ship"])

    assert result.exit_code == 0
    assert ship.call_args.args[0].parent != (
        tmp_path / "tracking" / "mystudy" / "submission"
    )
    assert not ship.call_args.args[0].exists()


def test_ship_with_scheme_less_server_addr_notes_and_succeeds(monkeypatch):
    monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "homelab:8000")
    with patch(
        "jernerics.commands.jobs.fetch_job_resources",
        return_value=JobResourcesResult([SNAPSHOT], None),
    ):
        result = CliRunner().invoke(app, ["job", "resources", "990001", "--ship"])

    assert result.exit_code == 0
    assert "Cannot ship resource record" in result.output


def test_ship_failure_leaves_exit_code_zero(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("JERNERICS_TRACKING_SERVER", "http://localhost:8000")
    monkeypatch.setattr("jernerics.commands.jobs.cache_dir", lambda: tmp_path)
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "990001.json").write_text(
        json.dumps({"job_id": "990001", "study_name": "mystudy"})
    )
    with (
        patch(
            "jernerics.commands.jobs.fetch_job_resources",
            return_value=JobResourcesResult([SNAPSHOT], None),
        ),
        patch("jernerics.commands.jobs.ship_events_file", return_value=False) as ship,
    ):
        result = CliRunner().invoke(app, ["job", "resources", "990001", "--ship"])

    assert result.exit_code == 0
    ship.assert_called_once()
    assert "shipped" not in result.output
