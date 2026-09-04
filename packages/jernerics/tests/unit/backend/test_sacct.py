import subprocess
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from jernerics.backend.adapter import build_job_resource_event
from jernerics.backend.slurm.sacct import (
    SACCT_FORMAT,
    fetch_job_resources,
    parse_duration_s,
    parse_mem_mb,
    parse_percent,
    parse_req_mem,
    parse_sacct_output,
)
from jernerics_schema import JobResourceEvent, TrackingEvent
from pydantic import TypeAdapter

COMPLETED_ROW = (
    "4242|COMPLETED|0:0|01:02:03|8|16Gc|2.5G|2.4G|4213.45%|28:10:00|"
    "cpu=8,mem=16G,billing=8|node[01-02]"
)


class TestParseDurationS:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("01:23:45", 5025.0),
            ("45:12", 2712.0),
            ("2-01:02:03", 2 * 86_400 + 3_723),
            ("90", 90.0),
            ("0:00", 0.0),
            ("1:00:00:00", None),
        ],
    )
    def test_values(self, text, expected):
        assert parse_duration_s(text) == expected

    @pytest.mark.parametrize("text", ["", "  ", "nonsense", "N/A", "-", "1-"])
    def test_degrades_to_none(self, text):
        assert parse_duration_s(text) is None


class TestParseMemMb:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1024K", 1.0),
            ("2G", 2048.0),
            ("1.5T", 1.5 * 1024.0**2),
            ("65536M", 65536.0),
        ],
    )
    def test_suffix_scaling(self, text, expected):
        assert parse_mem_mb(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "512", "12X", "N/A"])
    def test_degrades_to_none(self, text):
        assert parse_mem_mb(text) is None


class TestParseReqMem:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("16Gc", "16G"),
            ("16Gn", "16G"),
            ("16G", "16G"),
            ("48000Mn", "48000M"),
            ("7.8Gc", "7.8G"),
        ],
    )
    def test_modifier_dropped(self, text, expected):
        assert parse_req_mem(text) == expected

    @pytest.mark.parametrize("text", ["", "junk", "16", "16Gx"])
    def test_degrades_to_none(self, text):
        assert parse_req_mem(text) is None


class TestParsePercent:
    def test_strips_suffix(self):
        assert parse_percent("4213.45%") == pytest.approx(4213.45)

    def test_bare_number(self):
        assert parse_percent("98.54") == pytest.approx(98.54)

    @pytest.mark.parametrize("text", ["", "N/A", "%", "--"])
    def test_degrades_to_none(self, text):
        assert parse_percent(text) is None


class TestStepRowStripping:
    def test_batch_extern_and_step_rows_are_skipped(self):
        stdout = (
            f"{COMPLETED_ROW}\n"
            "4242.batch|COMPLETED|0:0|9:9:9|8|16Gc|3G|3G|1%|1:1:1||n\n"
            "4242.extern|COMPLETED|0:0|8:8:8|8|16Gc|0K|0K|0%|1:1:1||n\n"
            "4242.0|COMPLETED|0:0|7:7:7|8|16Gc|4G|4G|1%|1:1:1||n"
        )
        snapshot = parse_sacct_output("4242", stdout)

        assert snapshot is not None
        assert snapshot.wall_time_s == pytest.approx(3_723.0)
        assert snapshot.max_rss_mb == pytest.approx(2_560.0)

    def test_main_row_after_step_rows_is_used(self):
        stdout = f"4242.batch|COMPLETED|0:0|9:9:9\n{COMPLETED_ROW}"
        snapshot = parse_sacct_output("4242", stdout)

        assert snapshot is not None
        assert snapshot.wall_time_s == pytest.approx(3_723.0)

    def test_only_step_rows_yields_none(self):
        stdout = "4242.batch|COMPLETED|0:0\n4242.extern|COMPLETED|0:0\n4242.3|RUNNING"

        assert parse_sacct_output("4242", stdout) is None

    def test_empty_output_yields_none(self):
        assert parse_sacct_output("4242", "") is None


class TestParseSacctOutput:
    def test_full_row_maps_every_column(self):
        snapshot = parse_sacct_output("4242", COMPLETED_ROW)
        assert snapshot is not None
        assert snapshot.state == "COMPLETED"
        assert snapshot.exit_code == "0:0"
        assert snapshot.wall_time_s == pytest.approx(3_723.0)
        assert snapshot.cpu_time_s == pytest.approx(101_400.0)
        assert snapshot.cpu_pct == pytest.approx(4213.45)
        assert snapshot.max_rss_mb == pytest.approx(2_560.0)
        assert snapshot.ave_rss_mb == pytest.approx(2_457.6)
        assert snapshot.alloc_cpus == 8
        assert snapshot.req_mem == "16G"
        assert snapshot.alloc_tres == "cpu=8,mem=16G,billing=8"
        assert snapshot.node_list == "node[01-02]"

    def test_missing_columns_degrade_to_none(self):
        snapshot = parse_sacct_output("4242", "4242|RUNNING|")
        assert snapshot is not None
        assert snapshot.state == "RUNNING"
        assert snapshot.exit_code is None
        assert snapshot.wall_time_s is None
        assert snapshot.cpu_pct is None
        assert snapshot.max_rss_mb is None
        assert snapshot.alloc_cpus is None
        assert snapshot.alloc_tres is None
        assert snapshot.node_list is None

    def test_empty_field_values_degrade_to_none(self):
        row = "4242|||||||||||"
        snapshot = parse_sacct_output("4242", row)
        assert snapshot is not None
        assert snapshot.state is None
        assert snapshot.wall_time_s is None
        assert snapshot.alloc_cpus is None

    def test_array_element_row_keeps_requested_job_id(self):
        snapshot = parse_sacct_output("4242", "4242_7|COMPLETED|0:0|01:00:00")
        assert snapshot is not None
        assert snapshot.job_id == "4242"
        assert snapshot.state == "COMPLETED"

    def test_short_rows_are_ignored(self):
        assert parse_sacct_output("4242", "garbage") is None


class TestFetchJobResources:
    def test_invokes_sacct_with_bounded_timeout(self):
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=COMPLETED_ROW + "\n", stderr=""
        )
        with patch("jernerics.backend.slurm.sacct.subprocess.run") as run:
            run.return_value = completed
            result = fetch_job_resources("4242", timeout=7.0)

        run.assert_called_once()
        argv = run.call_args.args[0]
        assert argv[:4] == ["sacct", "-n", "-P", "-j", "4242"][:4]
        assert argv[4] == "4242"
        assert argv[5] == f"--format={SACCT_FORMAT}"
        assert run.call_args.kwargs["timeout"] == pytest.approx(7.0)
        assert result.error is None
        assert result.snapshot is not None
        assert result.snapshot.wall_time_s == pytest.approx(3_723.0)

    def test_timeout_returns_error_not_raise(self):
        with patch(
            "jernerics.backend.slurm.sacct.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="sacct", timeout=10),
        ):
            result = fetch_job_resources("4242")

        assert result.snapshot is None
        assert result.error and "timed out" in result.error

    def test_missing_sacct_binary_returns_error(self):
        with patch(
            "jernerics.backend.slurm.sacct.subprocess.run",
            side_effect=FileNotFoundError("sacct"),
        ):
            result = fetch_job_resources("4242")

        assert result.snapshot is None
        assert result.error and "unavailable" in result.error

    def test_nonzero_exit_returns_error(self):
        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="error: bad job\nsecond line"
        )
        with patch("jernerics.backend.slurm.sacct.subprocess.run") as run:
            run.return_value = failed
            result = fetch_job_resources("4242")

        assert result.snapshot is None
        assert result.error and "bad job" in result.error

    def test_fresh_job_without_rows_returns_error(self):
        empty = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with patch("jernerics.backend.slurm.sacct.subprocess.run") as run:
            run.return_value = empty
            result = fetch_job_resources("4242")

        assert result.snapshot is None
        assert result.error and "too fresh" in result.error


class TestBuildJobResourceEvent:
    def _snapshot(self):
        snapshot = parse_sacct_output("4242", COMPLETED_ROW)
        assert snapshot is not None
        return snapshot

    def test_event_id_is_deterministic_per_job(self):
        first = build_job_resource_event(self._snapshot())
        second = build_job_resource_event(
            self._snapshot(), study_name="other", submission_id="sub"
        )

        assert first.event_id == second.event_id

    def test_distinct_jobs_get_distinct_ids(self):
        other = parse_sacct_output("4243", COMPLETED_ROW.replace("4242", "4243"))
        assert other is not None

        assert (
            build_job_resource_event(self._snapshot()).event_id
            != build_job_resource_event(other).event_id
        )

    def test_roundtrips_through_tracking_event_union(self):
        recorded_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
        event = build_job_resource_event(
            self._snapshot(),
            study_name="study",
            submission_id=str(uuid.uuid4()),
            recorded_at=recorded_at,
        )

        parsed = TypeAdapter(TrackingEvent).validate_json(event.model_dump_json())

        assert isinstance(parsed, JobResourceEvent)
        assert parsed == event
        assert parsed.wall_time_s == pytest.approx(3_723.0)
        assert parsed.max_rss_mb == pytest.approx(2_560.0)
        assert parsed.req_mem == "16G"
        assert parsed.recorded_at == recorded_at
