"""Unit tests for observability analysis primitives.

Each test builds a synthetic :class:`jernerics_server.store.Store` (the
real SQLite schema) and exercises the analysis functions against it, so
the SQL and the math are both validated end to end.
"""

import tempfile
from pathlib import Path

import pytest
from jernerics.observability import (
    compute_metric_analysis,
    compute_slope,
    get_all_runs,
    get_metric_keys,
    get_metric_series,
    get_run_diff,
    get_run_summary,
    run_exists,
)
from jernerics_server.store import Store

PROJECT = "p"


@pytest.fixture
def store():
    s = Store(Path(tempfile.mkdtemp()) / "test.sqlite")
    yield s
    s.close()


def _env(seq, study, trial, kind, payload, ts=0, run_id=0):
    e = {
        "project": PROJECT,
        "study_name": study,
        "trial_id": trial,
        "run_id": run_id,
        "timestamp_ns": 1_000_000_000 + ts,
        "seq": seq,
    }
    e[kind] = payload
    return e


def _param(store, study, trial, key, value, *, seq=1, ts=1):
    if isinstance(value, bool):
        typed = {"bool_val": value}
    elif isinstance(value, int):
        typed = {"int_val": value}
    elif isinstance(value, float):
        typed = {"float_val": value}
    else:
        typed = {"string_val": value}
    store.insert_event(
        _env(seq, study, trial, "param", {"key": key, "value": typed}, ts=ts)
    )


def _series(store, study, trial, key, values, start_seq=100, start_ts=100):
    """Log ``values`` as one metric point per integer step 0..n-1."""
    for i, v in enumerate(values):
        store.insert_event(
            _env(
                start_seq + i,
                study,
                trial,
                "value",
                {"key": key, "value": float(v), "step": i},
                ts=start_ts + i,
            )
        )


class TestComputeSlope:
    def test_linear(self):
        slope = compute_slope(list(range(10)), [2 * i for i in range(10)], 0, 10)
        assert slope == pytest.approx(2.0)

    def test_flat(self):
        slope = compute_slope(list(range(10)), [5.0] * 10, 0, 10)
        assert slope == pytest.approx(0.0)

    def test_negative(self):
        slope = compute_slope(list(range(10)), [10 - i for i in range(10)], 0, 10)
        assert slope == pytest.approx(-1.0)

    def test_window_slice_uses_only_window(self):
        # Flat prefix then linear tail; slope over the tail only.
        steps = list(range(20))
        values = [0.0] * 10 + [float(i) for i in range(10)]
        slope = compute_slope(steps, values, 10, 20)
        assert slope == pytest.approx(1.0)

    def test_too_few_points_returns_zero(self):
        assert compute_slope([1], [5.0], 0, 1) == pytest.approx(0.0)
        assert compute_slope([], [], 0, 0) == pytest.approx(0.0)

    def test_constant_steps_returns_zero(self):
        # All steps equal -> no x-range -> 0.0 (not a div-by-zero).
        slope = compute_slope([3, 3, 3], [1.0, 2.0, 3.0], 0, 3)
        assert slope == pytest.approx(0.0)


class TestMetricAnalysis:
    def test_first_last_change_extraction(self, store):
        _series(store, "s", 0, "loss", [2.0, 1.5, 1.0, 0.5, 0.2])
        m = compute_metric_analysis(store, PROJECT, "s", 0, "loss")
        assert m["first"] == pytest.approx(2.0)
        assert m["last"] == pytest.approx(0.2)
        assert m["change"] == pytest.approx(-1.8)
        assert m["n_points"] == 5

    def test_min_points_guard_skips_slopes(self, store):
        # 4 points -> 10% window (0) below the 5-point floor -> skip slopes.
        _series(store, "s", 0, "loss", [2.0, 1.5, 1.0, 0.5])
        m = compute_metric_analysis(store, PROJECT, "s", 0, "loss")
        assert m["early_slope"] is None
        assert m["recent_slope"] is None
        assert m["early_range"] is None
        assert m["recent_range"] is None
        # first/last/change are still derived from the whole series.
        assert m["first"] == pytest.approx(2.0)
        assert m["last"] == pytest.approx(0.5)

    def test_guard_skips_when_window_below_floor(self, store):
        # 40 points -> 10% window = 4 < 5 -> slopes skipped even though
        # the series is long enough to have *some* trend.
        _series(store, "s", 0, "loss", [1.0 - 0.01 * i for i in range(40)])
        m = compute_metric_analysis(store, PROJECT, "s", 0, "loss")
        assert m["early_slope"] is None
        assert m["recent_slope"] is None

    def test_slopes_on_linear_decreasing(self, store):
        # 50 points -> 10% window = 5 -> slopes computed, each ~= -0.1/step.
        _series(store, "s", 0, "loss", [2.0 - 0.1 * i for i in range(50)])
        m = compute_metric_analysis(store, PROJECT, "s", 0, "loss")
        assert m["early_slope"] == pytest.approx(-0.1, abs=1e-9)
        assert m["recent_slope"] == pytest.approx(-0.1, abs=1e-9)
        assert m["early_range"] == [0, 4]
        assert m["recent_range"] == [45, 49]

    def test_empty_metric(self, store):
        m = compute_metric_analysis(store, PROJECT, "s", 0, "nope")
        assert m["first"] is None
        assert m["last"] is None
        assert m["change"] is None
        assert m["n_points"] == 0


class TestGetAllRuns:
    def test_status_priority_and_params(self, store):
        _param(store, "alpha", 0, "lr", 0.001, seq=1)
        _param(store, "alpha", 0, "d_model", 512, seq=2)
        _series(store, "alpha", 0, "loss", [3.0, 2.0, 1.0])
        store.insert_event(_env(999, "alpha", 0, "trial_end", {}, ts=500))
        _series(store, "beta", 0, "loss", [5.0, 4.0])  # no trial_end -> running

        runs = {r["study_name"]: r for r in get_all_runs(store, PROJECT)}
        assert runs["alpha"]["status"] == "completed"
        assert runs["beta"]["status"] == "running"
        assert runs["alpha"]["priority_key"] == "loss"
        assert runs["alpha"]["priority_value"] == pytest.approx(1.0)
        assert runs["alpha"]["params"]["d_model"] == 512
        assert runs["alpha"]["min_step"] == 0
        assert runs["alpha"]["max_step"] == 2

    def test_priority_metric_falls_back_through_candidates(self, store):
        # No loss/error/accuracy/r2 -> priority_key None, column omitted.
        _series(store, "s", 0, "f1", [0.5, 0.6])
        runs = get_all_runs(store, PROJECT)
        assert runs[0]["priority_key"] is None
        assert runs[0]["priority_value"] is None

    def test_empty_store(self, store):
        assert get_all_runs(store, PROJECT) == []


class TestRunSummary:
    def test_includes_metrics_params_artifacts(self, store):
        _param(store, "s", 0, "lr", 0.1, seq=1)
        _series(store, "s", 0, "loss", [1.0 - 0.01 * i for i in range(50)])
        store.insert_event(
            _env(500, "s", 0, "artifact", {"key": "model.pt", "filename": "model.pt"})
        )
        s = get_run_summary(store, PROJECT, "s", 0)
        assert s["status"] == "running"
        assert set(s["metrics"]) == {"loss"}
        assert s["params"] == {"lr": 0.1}
        assert s["artifacts"] == ["model.pt"]


class TestRunSummaryGitHash:
    def test_returns_git_hash_from_sweep_meta(self, store):
        _param(store, "s", 0, "lr", 0.1, seq=1)
        store.insert_event(
            _env(
                10,
                "s",
                0,
                "sweep_meta",
                {"git_hash": "4a5e1097211230273aa1888d482bab2885990fb5", "config": ""},
                ts=10,
            )
        )
        s = get_run_summary(store, PROJECT, "s", 0)
        assert s["git_hash"] == "4a5e1097211230273aa1888d482bab2885990fb5"

    def test_returns_none_when_no_sweep_meta(self, store):
        _param(store, "s", 0, "lr", 0.1, seq=1)
        s = get_run_summary(store, PROJECT, "s", 0)
        assert s["git_hash"] is None

    def test_returns_none_when_git_hash_null(self, store):
        _param(store, "s", 0, "lr", 0.1, seq=1)
        store.insert_event(
            _env(10, "s", 0, "sweep_meta", {"git_hash": None, "config": ""}, ts=10)
        )
        s = get_run_summary(store, PROJECT, "s", 0)
        assert s["git_hash"] is None


class TestRunSummaryTextMetrics:
    def test_lists_keys_and_counts(self, store):
        _series(store, "s", 0, "loss", [1.0, 0.5])
        for i in range(5):
            store.insert_event(
                _env(
                    200 + i,
                    "s",
                    0,
                    "value",
                    {"key": "pred_expr", "value_json": f'"expr{i}"', "step": i},
                    ts=200 + i,
                )
            )
        for i in range(3):
            store.insert_event(
                _env(
                    300 + i,
                    "s",
                    0,
                    "value",
                    {"key": "confusion", "value_json": '{"tp": 1}', "step": i},
                    ts=300 + i,
                )
            )
        s = get_run_summary(store, PROJECT, "s", 0)
        text_metrics = {tm["key"]: tm["n_points"] for tm in s["text_metrics"]}
        assert text_metrics == {"pred_expr": 5, "confusion": 3}

    def test_empty_when_no_json_values(self, store):
        _series(store, "s", 0, "loss", [1.0, 0.5])
        s = get_run_summary(store, PROJECT, "s", 0)
        assert s["text_metrics"] == []


class TestRunDiff:
    def test_different_metric_sets(self, store):
        # alpha has loss + acc; beta has loss only. Distinct seq ranges so
        # the per-(run,seq) uniqueness constraint does not drop a metric.
        _series(store, "alpha", 0, "loss", [2.0, 1.0], start_seq=100)
        _series(store, "alpha", 0, "acc", [0.1, 0.2], start_seq=200)
        _series(store, "beta", 0, "loss", [3.0, 2.0], start_seq=300)
        diff = get_run_diff(store, PROJECT, "alpha", "beta")
        keys = {m["key"]: m for m in diff["metric_diff"]}
        # acc present only in alpha -> b is None, change None.
        assert keys["acc"]["a"] == pytest.approx(0.2)
        assert keys["acc"]["b"] is None
        assert keys["acc"]["change"] is None
        # loss present in both -> change = b_last - a_last.
        assert keys["loss"]["a"] == pytest.approx(1.0)
        assert keys["loss"]["b"] == pytest.approx(2.0)
        assert keys["loss"]["change"] == pytest.approx(1.0)

    def test_param_diff_and_match(self, store):
        _param(store, "a", 0, "lr", 0.1, seq=1)
        _param(store, "a", 0, "shared", 8, seq=2)
        _param(store, "b", 0, "lr", 0.01, seq=1)
        _param(store, "b", 0, "shared", 8, seq=2)

        diff = get_run_diff(store, PROJECT, "a", "b")
        diffs = {d["key"]: d for d in diff["param_diff"]}
        assert diffs["lr"]["a"] == pytest.approx(0.1)
        assert diffs["lr"]["b"] == pytest.approx(0.01)
        assert diff["param_match"] == ["shared"]
        assert diff["param_match_count"] == 1


class TestRunExists:
    def test_existing_and_missing(self, store):
        _param(store, "s", 0, "lr", 0.1, seq=1)
        assert run_exists(store, PROJECT, "s", 0) is True
        assert run_exists(store, PROJECT, "ghost", 0) is False

    def test_trial_end_counts(self, store):
        store.insert_event(_env(1, "s", 0, "trial_end", {}))
        assert run_exists(store, PROJECT, "s", 0) is True


def _text_series(store, study, trial, key, values, start_seq=100, start_ts=100):
    """Log text values as one json metric point per integer step 0..n-1."""
    for i, v in enumerate(values):
        store.insert_event(
            _env(
                start_seq + i,
                study,
                trial,
                "value",
                {"key": key, "value_json": v, "step": i},
                ts=start_ts + i,
            )
        )


class TestGetMetricSeries:
    def test_scalar_returns_values_ordered_by_seq(self, store):
        _series(store, "s", 0, "loss", [9.0, 3.0, 0.8, 0.05, 0.03])
        result = get_metric_series(store, PROJECT, "s", 0, "loss")
        assert result is not None
        assert result["value_type"] == "scalar"
        series = result["series"]
        assert len(series) == 5
        assert [p["value"] for p in series] == pytest.approx(
            [9.0, 3.0, 0.8, 0.05, 0.03]
        )
        assert [p["step"] for p in series] == [0, 1, 2, 3, 4]
        assert [p["seq"] for p in series] == [100, 101, 102, 103, 104]

    def test_text_returns_text_values(self, store):
        _text_series(store, "s", 0, "pred_expr", ["<BOS>", "mul add", "final"])
        result = get_metric_series(store, PROJECT, "s", 0, "pred_expr")
        assert result is not None
        assert result["value_type"] == "json"
        series = result["series"]
        assert len(series) == 3
        assert [p["value"] for p in series] == ["<BOS>", "mul add", "final"]
        assert [p["step"] for p in series] == [0, 1, 2]

    def test_nonexistent_metric_returns_none(self, store):
        _series(store, "s", 0, "loss", [1.0])
        result = get_metric_series(store, PROJECT, "s", 0, "ghost_metric")
        assert result is None

    def test_nonexistent_run_returns_none(self, store):
        result = get_metric_series(store, PROJECT, "ghost", 0, "loss")
        assert result is None

    def test_includes_timestamp_ns(self, store):
        _series(store, "s", 0, "loss", [1.0, 0.5], start_ts=200)
        result = get_metric_series(store, PROJECT, "s", 0, "loss")
        assert result is not None
        assert result["series"][0]["timestamp_ns"] == 1_000_000_200
        assert result["series"][1]["timestamp_ns"] == 1_000_000_201


class TestGetMetricKeys:
    def test_returns_scalar_and_text_keys(self, store):
        _series(store, "s", 0, "loss", [1.0, 2.0], start_seq=100)
        _text_series(store, "s", 0, "pred_expr", ["a", "b"], start_seq=200)
        keys = get_metric_keys(store, PROJECT, "s", 0)
        assert len(keys) == 2
        loss_entry = next(k for k in keys if k["key"] == "loss")
        assert loss_entry["value_type"] == "scalar"
        assert loss_entry["count"] == 2
        pred_entry = next(k for k in keys if k["key"] == "pred_expr")
        assert pred_entry["value_type"] == "json"
        assert pred_entry["count"] == 2

    def test_nonexistent_run_returns_empty(self, store):
        keys = get_metric_keys(store, PROJECT, "ghost", 0)
        assert keys == []
