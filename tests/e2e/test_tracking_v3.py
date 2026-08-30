"""Deterministic end-to-end proof of the complete Tracking v3 system.

One scenario, built once per module by the ``scenario`` fixture, exercises
every locked user surface in order: deploy-path submission events, two
runner trials (a failing one and its successful retry, with real retry
planning and lineage), live streaming plus post-hook replay and
reconciliation, immutable artifact versions with stored stdout/stderr,
the typed client, and the dashboard service layer. The browser pass over
the mounted dashboard happens out-of-band (h5d.11-14 proved the pages;
here the QueryService facts underneath them are asserted).
"""

import json
import socket
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid5

import httpx
import optuna
import pytest
import uvicorn
from jernerics.backend.host import LocalHost
from jernerics.backend.models import JobSubmission, SubmitResult, SweepSubmission
from jernerics.backend.submission import (
    build_submission_events,
    write_submission_events,
)
from jernerics.post_hook import PipelineResult, run_pipeline
from jernerics.retry import RetryContext, plan_retry, read_ledger, write_ledger
from jernerics.retry_checker import _enqueue_retry
from jernerics.runner import run_trial
from jernerics.tracking import TrackingClient
from jernerics.tracking.batch_sync import replay_tracking, ship_events_file
from jernerics.tracking.jsonl_io import scan_events
from jernerics_schema import (
    JERNERICS_NAMESPACE,
    PROTOCOL_VERSION,
    IngestRequest,
    JobResourceEvent,
    Selection,
    SubmissionSnapshotEvent,
    decode_selection,
    encode_selection,
    sweep_id_for,
)
from jernerics_schema.ingest import MAX_EVENTS_PER_REQUEST
from jernerics_server.dashboard.service import DashboardService
from jernerics_server.http import create_app
from jernerics_server.ingest import IngestService
from jernerics_server.queries import QueryService
from jernerics_server.store import Store
from optuna.storages.journal import JournalFileBackend, JournalStorage

optuna.logging.set_verbosity(optuna.logging.WARNING)

PROJECT = "proj"
SWEEP = "tracking-v3-e2e"
STALE_TRIAL_ID = UUID(int=0xE2E2)

TRIAL_SCRIPT = """\
import os
import sys
import time
from pathlib import Path
from uuid import UUID

from jernerics import trial_config, trial_tracker
from jernerics.tracking.tracker import JsonlTracker

config = trial_config()
tracker = trial_tracker()
number = os.environ["JERNERICS_TRIAL_NUMBER"]
root = Path(os.environ["JERNERICS_TRACKING_DIR"])

tracker.log_param("batch_size", 32)
for step in range(3):
    tracker.log_value("loss", 1.0 - 0.25 * step, step=step)
tracker.log_value("summary", {"accuracy": 0.75, "rate": config["rate"]})

raw = JsonlTracker(
    root / "events" / f"{number}.jsonl",
    UUID(os.environ["JERNERICS_TRIAL_ID"]),
    UUID(os.environ["JERNERICS_EXECUTION_ID"]),
)
raw.set_progress(3, 3, "steps")
time.sleep(0.15)

out = root.parent / "artifacts-out"
out.mkdir(exist_ok=True)
first = out / f"model-v1-{number}.txt"
first.write_text(f"model-v1-{number}")
tracker.log_artifact("model", str(first))
second = out / f"model-v2-{number}.txt"
second.write_text(f"model-v2-{number}")
tracker.log_artifact("model", str(second))

print(f"trial {number} stdout")
print(f"trial {number} stderr", file=sys.stderr)

if int(number) == 0:
    raise SystemExit(3)

tracker.finish({"loss": 0.25})
"""

PYPROJECT_SOURCE = """\
[project]
name = "proj"
version = "0.1.0"

[tool.jernerics.backends.slurm]
type = "slurm"
grace_period_s = 0
stale_after_s = 0
fast_fail_threshold_s = 0
max_retries = 2
"""

CONFIG_SOURCE = """\
base = {"batch_note": "e2e"}
n_trials = 1

def search_space(trial):
    return {
        "rate": trial.suggest_float("rate", 0.1, 0.9),
        "seed": trial.suggest_int("seed", 1, 5),
        "mode": trial.suggest_categorical("mode", ["a", "b"]),
    }

def objective(results):
    return results["loss"]
"""

COUNT_TABLES = (
    "sweeps",
    "submissions",
    "submission_jobs",
    "trials",
    "trial_params",
    "executions",
    "execution_progress",
    "tracked_values",
    "artifacts",
    "reconciliation_conflicts",
    "job_resources",
)


def _row_counts(db_path: Path) -> dict[str, int]:
    with Store(db_path) as store:
        return {
            table: store.query(f"SELECT COUNT(*) FROM {table}")[1][0][0]
            for table in COUNT_TABLES
        }


def _one(db_path: Path, sql: str, params: list | None = None):
    with Store(db_path) as store:
        return store.query(sql, params)[1]


def _trial_id(scenario, number: int) -> UUID:
    return UUID(
        _one(
            scenario.db_path,
            "SELECT trial_id FROM trials WHERE number = ?",
            [number],
        )[0][0]
    )


@pytest.fixture(scope="module")
def scenario(tmp_path_factory, request):
    """Build the whole world once and drive it through the real pipeline."""
    tmp_path = tmp_path_factory.mktemp("tracking-v3")
    base_url, db_path, artifacts_root, server, thread, store = _start_server(tmp_path)

    def _teardown_server():
        server.should_exit = True
        thread.join(timeout=5)
        store.close()
        assert not thread.is_alive()

    request.addfinalizer(_teardown_server)

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(PYPROJECT_SOURCE)
    config_file = project_dir / "config.py"
    config_file.write_text(CONFIG_SOURCE)
    trial_file = project_dir / "trial.py"
    trial_file.write_text(TRIAL_SCRIPT)

    storage_url = str(tmp_path / "sweep.journal")
    tracking_dir = tmp_path / "tracking" / SWEEP
    tracking_dir.mkdir(parents=True)

    spec = SweepSubmission(
        trial_path=trial_file,
        config_path=config_file,
        study_name=SWEEP,
        storage_url=storage_url,
        n_trials=1,
        trial_relpath="trial.py",
        config_relpath="config.py",
        project_name=PROJECT,
        git_hash="deadbeef",
    )
    submit_result = SubmitResult(
        submissions=[
            JobSubmission(job_id="990001", role="trials", n_trials=1),
            JobSubmission(job_id="990002", role="post_hook"),
        ]
    )
    submission_events = build_submission_events(spec, "slurm", submit_result)
    write_submission_events(
        submission_events, LocalHost(), str(tracking_dir), "deploy.jsonl"
    )

    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(
        RetryContext(
            study_name=SWEEP,
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            storage_path=storage_url,
            tracking_dir=str(tracking_dir),
            project_dir=str(project_dir),
            project_name=PROJECT,
            host_home=str(tmp_path),
        ).to_json()
    )

    study = optuna.create_study(
        study_name=SWEEP,
        storage=JournalStorage(JournalFileBackend(storage_url)),
        sampler=optuna.samplers.TPESampler(seed=7),
    )

    # Deploy-time shipping (what Backend.prepare_and_submit and
    # LocalBackend.submit_sweep do right after the scheduler submission
    # returns): the sweep and submission snapshots land before any trial
    # streams live, so ingest validates the first live batch immediately
    # instead of 409-retrying until the post-hook replay.
    assert ship_events_file(tracking_dir / "submission" / "deploy.jsonl", base_url)

    def _run_runner_trial():
        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name=SWEEP,
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name=PROJECT,
            server_addr=base_url,
            heartbeat_interval_s=0.05,
        )

    with pytest.raises(SystemExit) as caught:
        _run_runner_trial()
    assert caught.value.code == 1
    failed_trial_events = list(scan_events(tracking_dir / "events" / "0.jsonl", 0)[0])
    live_rows_after_first_trial = _row_counts(db_path)

    stale = study.ask()
    stale.suggest_float("rate", 0.25, 0.25)
    stale.suggest_int("seed", 2, 2)
    stale.suggest_categorical("mode", ["a", "b"])
    stale.set_user_attr("jernerics_trial_id", str(STALE_TRIAL_ID))

    ledger_path = tracking_dir / ".retry_ledger.json"
    plan = plan_retry(
        trials=study.trials,
        heartbeats_dir=tracking_dir / "heartbeats",
        ledger=read_ledger(ledger_path),
        n_trials=1,
        stale_after=0,
        max_retries=2,
        now=time.time(),
        fast_fail_threshold_s=0,
    )
    assert plan.stale_trial_ids == [1]
    sweep_id = sweep_id_for(PROJECT, SWEEP)
    _enqueue_retry(
        study, 1, sweep_id=sweep_id, submission_dir=tracking_dir / "submission"
    )
    write_ledger(ledger_path, plan.retry_counts)

    # The checker ships its failed-trial snapshot at enqueue time (what
    # run_checker does after write_ledger): the retry trial's live
    # snapshot references its retry parent, which must exist server-side
    # before the retry streams.
    assert ship_events_file(tracking_dir / "submission" / "checker.jsonl", base_url)

    _run_runner_trial()

    live_events = [event for event, _ in failed_trial_events] + [
        event
        for path in sorted((tracking_dir / "events").glob("*.jsonl"))
        for event, _ in scan_events(path, 0)[0]
    ]

    result = run_pipeline(
        ctx_path=str(ctx_path),
        chain_depth=0,
        tracking_dir=str(tracking_dir),
        base_url=base_url,
    )
    assert result == PipelineResult.SWEEP_COMPLETE

    yield SimpleNamespace(
        base_url=base_url,
        db_path=db_path,
        artifacts_root=artifacts_root,
        tracking_dir=tracking_dir,
        storage_url=storage_url,
        ctx_path=ctx_path,
        spec=spec,
        submission_events=submission_events,
        live_events=live_events,
        live_rows_after_first_trial=live_rows_after_first_trial,
    )


def _start_server(
    tmp_path: Path,
) -> tuple[str, Path, Path, uvicorn.Server, threading.Thread, Store]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db_path = tmp_path / "server.sqlite"
    artifacts_root = tmp_path / "artifacts"
    store = Store(db_path)
    app = create_app(store, artifacts_root=artifacts_root)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", db_path, artifacts_root, server, thread, store


class TestSubmissionEvents:
    def test_deploy_path_events_materialize_sweep_submission_and_jobs(self, scenario):
        sweep_id = sweep_id_for(PROJECT, SWEEP)
        assert _one(
            scenario.db_path,
            "SELECT project, name, state FROM sweeps WHERE sweep_id = ?",
            [str(sweep_id)],
        ) == [(PROJECT, SWEEP, "running")]

        submission = _one(
            scenario.db_path,
            "SELECT backend, state, expected_trials, git_hash, config_source "
            "FROM submissions",
        )
        assert submission == [("slurm", "submitted", 1, "deadbeef", "config.py")]

        jobs = _one(
            scenario.db_path,
            "SELECT scheduler_job_id, role, state FROM submission_jobs "
            "ORDER BY scheduler_job_id",
        )
        assert jobs == [
            ("990001", "trials", "submitted"),
            ("990002", "post_hook", "submitted"),
        ]

    def test_first_trial_materializes_live_without_replay(self, scenario):
        """Captured right after the failing trial's runner returned:
        every live-shipped fact is already queryable — no replay ever
        ran before this point, only the deploy-time event ship."""
        rows = scenario.live_rows_after_first_trial
        assert rows["sweeps"] == 1
        assert rows["submissions"] == 1
        assert rows["submission_jobs"] == 2
        assert rows["trials"] == 1
        assert rows["executions"] == 1
        assert rows["tracked_values"] >= 4
        assert rows["trial_params"] >= 4


class TestFailureRetryLineage:
    def test_terminal_states_match_the_optuna_journal(self, scenario):
        study = optuna.load_study(
            study_name=SWEEP,
            storage=JournalStorage(JournalFileBackend(scenario.storage_url)),
        )
        rows = {
            number: (state, objective)
            for number, state, objective in _one(
                scenario.db_path,
                "SELECT number, state, objective FROM trials",
            )
        }
        assert set(rows) == {0, 1, 2}
        for frozen in study.trials:
            state, objective = rows[frozen.number]
            expected = (
                "completed"
                if frozen.state == optuna.trial.TrialState.COMPLETE
                else "failed"
            )
            assert state == expected
            if frozen.state == optuna.trial.TrialState.COMPLETE:
                assert objective == pytest.approx(frozen.value)

    def test_retry_family_lineage_two_generations(self, scenario):
        lineage = _one(
            scenario.db_path,
            "SELECT trial_id, number, retry_of_trial_id, retry_root_trial_id, "
            "retry_index FROM trials ORDER BY number",
        )
        assert [(row[1], row[4]) for row in lineage] == [(0, 0), (1, 0), (2, 1)]
        stale_root, retry = lineage[1], lineage[2]
        assert stale_root[2] is None
        assert UUID(stale_root[3]) == STALE_TRIAL_ID
        assert UUID(retry[2]) == STALE_TRIAL_ID
        assert UUID(retry[3]) == STALE_TRIAL_ID
        with Store(scenario.db_path) as store:
            queries = QueryService(store, heartbeat_stale_s=900.0)
            families = queries.trial_families(
                Selection(project=PROJECT, retry_roots=(STALE_TRIAL_ID,))
            )
        assert len(families) == 1
        assert families[0]["generations"] == 2
        assert UUID(families[0]["current_trial"]) == UUID(retry[0])

    def test_failed_execution_keeps_facts(self, scenario):
        outcome, exit_code, summary = _one(
            scenario.db_path,
            "SELECT outcome, exit_code, failure_summary FROM executions e "
            "JOIN trials t ON t.trial_id = e.trial_id WHERE t.number = 0",
        )[0]
        assert outcome == "failure"
        assert exit_code == 3
        assert "exited with code 3" in summary


class TestValuesParamsProgress:
    def test_manual_param_persists_distinct_from_sampled(self, scenario):
        rows = {
            key: kind
            for key, kind in _one(
                scenario.db_path,
                "SELECT p.key, p.kind FROM trial_params p "
                "JOIN trials t ON t.trial_id = p.trial_id WHERE t.number = 2",
            )
        }
        assert rows["batch_size"] == "manual"
        assert {rows["rate"], rows["seed"], rows["mode"]} == {"sampled"}

    def test_scalar_series_json_observation_and_context(self, scenario):
        rows = _one(
            scenario.db_path,
            "SELECT step, scalar_val FROM tracked_values v "
            "JOIN executions e ON e.execution_id = v.execution_id "
            "JOIN trials t ON t.trial_id = e.trial_id "
            "WHERE v.key = 'loss' AND t.number = 2 ORDER BY step",
        )
        assert rows == [(0, pytest.approx(1.0)), (1, 0.75), (2, 0.5)]

        observation = _one(
            scenario.db_path,
            "SELECT text_val FROM tracked_values "
            "WHERE key = 'summary' AND text_val IS NOT NULL",
        )
        assert observation
        assert json.loads(observation[0][0])["accuracy"] == pytest.approx(0.75)

    def test_progress_and_heartbeats_recorded(self, scenario):
        progress = _one(
            scenario.db_path,
            "SELECT current, total, unit FROM execution_progress",
        )
        assert progress == [(3, 3, "steps"), (3, 3, "steps")]

        heartbeats = _one(
            scenario.db_path,
            "SELECT COUNT(*) FROM executions WHERE last_heartbeat_ns IS NOT NULL",
        )
        assert heartbeats == [(2,)]


class TestArtifactsAndStoredLogs:
    def test_repeated_key_yields_two_immutable_versions(self, scenario):
        rows = _one(
            scenario.db_path,
            "SELECT a.key, a.filename, a.source, a.received_ns IS NOT NULL, "
            "b.sha256 FROM artifacts a LEFT JOIN artifact_blobs b "
            "ON b.artifact_id = a.artifact_id "
            "JOIN trials t ON t.trial_id = a.trial_id "
            "WHERE a.key = 'model' AND t.number = 2 ORDER BY a.declared_ns",
        )
        assert [row[1] for row in rows] == ["model-v1-2.txt", "model-v2-2.txt"]
        assert all(row[2] == "user" and row[3] == 1 and row[4] for row in rows)

        with TrackingClient(scenario.base_url) as client:
            project = client.project(PROJECT)
            artifacts = project.artifacts(
                project.for_trials(_trial_id(scenario, 2)), keys=("model",)
            )
            assert len(artifacts) == 2
            texts = sorted(
                client.download(
                    record,
                    scenario.db_path.parent / f"dl-{record.artifact_id.hex}.txt",
                ).read_text()
                for record in artifacts
            )
        assert texts == ["model-v1-2", "model-v2-2"]

    def test_stored_stdout_stderr_are_downloadable(self, scenario):
        rows = _one(
            scenario.db_path,
            "SELECT a.key, a.filename FROM artifacts a "
            "WHERE a.source = 'system' AND a.key IN ('stdout', 'stderr') "
            "ORDER BY a.key",
        )
        assert {row[0] for row in rows} == {"stdout", "stderr"}
        with TrackingClient(scenario.base_url) as client:
            project = client.project(PROJECT)
            for record in project.artifacts(
                project.selection(), keys=("stdout", "stderr"), source="system"
            ):
                dest = scenario.db_path.parent / f"log-{record.artifact_id.hex}.txt"
                client.download(record, dest)
                assert record.key in dest.read_text()


class TestLiveReplayOverlap:
    def test_second_pass_changes_no_state_and_records_the_overlap(self, scenario):
        """Re-applying the live-shipped log (what a replay with a lost
        cursor sends) recognizes every event as already seen — except the
        pre-terminal RUNNING snapshots of now-terminal trials, which are
        recorded as optimizer_terminal_state conflicts and never
        overwrite the terminal facts."""
        counts_before = _row_counts(scenario.db_path)
        duplicates = 0
        conflicts = []
        with Store(scenario.db_path) as store:
            service = IngestService(store)
            for start in range(0, len(scenario.live_events), MAX_EVENTS_PER_REQUEST):
                result = service.apply(
                    IngestRequest(
                        protocol_version=PROTOCOL_VERSION,
                        events=scenario.live_events[
                            start : start + MAX_EVENTS_PER_REQUEST
                        ],
                    )
                )
                duplicates += result.duplicates
                conflicts.extend(result.conflicts)
        assert duplicates == len(scenario.live_events) - 2
        assert sorted(conflict.detail for conflict in conflicts) == [
            '{"existing":"completed","incoming":"running"}',
            '{"existing":"failed","incoming":"running"}',
        ]
        assert _row_counts(scenario.db_path) == {
            **counts_before,
            "reconciliation_conflicts": counts_before["reconciliation_conflicts"] + 2,
        }


class TestTypedClient:
    def test_full_round_trip(self, scenario):
        with TrackingClient(scenario.base_url) as client:
            assert client.projects() == [PROJECT]
            project = client.project(PROJECT)

            sweeps = project.sweeps()
            assert [sweep.name for sweep in sweeps] == [SWEEP]

            trials = sorted(project.trials(), key=lambda trial: trial.number)
            assert [trial.number for trial in trials] == [0, 1, 2]
            assert [trial.state for trial in trials] == [
                "failed",
                "failed",
                "completed",
            ]

            lineage = sorted(
                project.lineage(project.for_retry_roots(STALE_TRIAL_ID)),
                key=lambda record: record.retry_index,
            )
            assert [record.retry_index for record in lineage] == [0, 1]
            assert lineage[1].retry_of_trial_id == STALE_TRIAL_ID

            executions = project.executions(states=("ended",))
            assert {record.outcome for record in executions} == {"failure", "success"}
            failed = [record for record in executions if record.outcome == "failure"]
            assert {record.exit_code for record in failed} == {3}

            latest = project.latest_values(project.for_trials(trials[2].trial_id))
            assert latest["loss"].value == pytest.approx(0.5)
            assert latest["summary"].observation["accuracy"] == pytest.approx(0.75)

            assert project.reduce("loss", fn="last", selection=None) == pytest.approx(
                0.5
            )

            params = project.params(
                project.for_trials(trials[2].trial_id), kinds=("manual",)
            )
            assert [(param.key, param.value) for param in params] == [
                ("batch_size", 32)
            ]

            provenance = project.provenance(project.selection())
            assert provenance and provenance[0].backend == "slurm"

            selection = project.selection()
            assert decode_selection(encode_selection(selection)) == selection

            raw = client.raw_query("SELECT number, state FROM trials ORDER BY number")
            assert raw["rows"] == [[0, "failed"], [1, "failed"], [2, "completed"]]


class TestDashboardServiceFacts:
    def test_monitoring_counts(self, scenario):
        with Store(scenario.db_path) as store:
            service = DashboardService(queries=QueryService(store))
            rows = service.sweep_overview(PROJECT, ())
        assert len(rows) == 1
        overview = rows[0]
        assert overview.name == SWEEP
        assert overview.backend == "slurm"
        assert overview.submitted_jobs == 2
        assert overview.expected_trials == 1
        assert (overview.started, overview.terminal) == (2, 2)
        assert (overview.succeeded, overview.failed) == (1, 1)
        assert (overview.active, overview.quiet, overview.stale) == (0, 0, 0)
        assert not overview.incomplete

    def test_catalog(self, scenario):
        with Store(scenario.db_path) as store:
            service = DashboardService(queries=QueryService(store))
            keys = {
                row["key"]: row for row in service.analysis_value_keys(PROJECT, None)
            }
        assert keys["loss"]["kind"] == "scalar"
        assert keys["loss"]["steps"]
        assert keys["summary"]["kind"] == "json"

    def test_series(self, scenario):
        with Store(scenario.db_path) as store:
            service = DashboardService(queries=QueryService(store))
            grouped = service.analysis_series(PROJECT, None, ["loss", "missing"])
        assert [entry["key"] for entry in grouped] == ["loss", "missing"]
        loss, missing = grouped
        assert missing["series"] == []
        assert len(loss["series"]) == 2
        for series in loss["series"]:
            assert series["points"] == [
                (0, pytest.approx(1.0)),
                (1, 0.75),
                (2, 0.5),
            ]
        assert len({series["execution"] for series in loss["series"]}) == 2


class TestJobResources:
    def _submission_id(self, scenario) -> str:
        return str(
            next(
                event
                for event in scenario.submission_events
                if isinstance(event, SubmissionSnapshotEvent)
            ).submission_id
        )

    def _ingest(self, scenario, event) -> dict:
        response = httpx.post(
            f"{scenario.base_url}/ingest",
            json={
                "protocol_version": PROTOCOL_VERSION,
                "events": [event.model_dump(mode="json")],
            },
        )
        assert response.status_code == 200
        return response.json()

    def test_ingest_and_query_back(self, scenario):
        event = JobResourceEvent(
            event_id=uuid5(JERNERICS_NAMESPACE, "job-resource:990001"),
            recorded_at=datetime.now(UTC),
            job_id="990001",
            study_name=SWEEP,
            submission_id=self._submission_id(scenario),
            wall_time_s=3_723.0,
            cpu_time_s=101_400.0,
            cpu_pct=4213.45,
            max_rss_mb=2_560.0,
            ave_rss_mb=2_457.6,
            alloc_cpus=8,
            req_mem="16G",
            alloc_tres="cpu=8,mem=16G,billing=8",
            node_list=None,
            state="COMPLETED",
            exit_code="0:0",
        )
        assert self._ingest(scenario, event)["accepted"] == 1

        body = httpx.post(
            f"{scenario.base_url}/job-resources",
            json={
                "selection": {
                    "project": PROJECT,
                    "sweeps": [str(sweep_id_for(PROJECT, SWEEP))],
                }
            },
        ).json()

        assert len(body["records"]) == 1
        record = body["records"][0]
        assert record["job_id"] == "990001"
        assert record["study_name"] == SWEEP
        assert record["wall_time_s"] == pytest.approx(3_723.0)
        assert record["max_rss_mb"] == pytest.approx(2_560.0)
        assert record["req_mem"] == "16G"
        assert record["node_list"] is None

    def test_recapture_is_idempotent_and_fills_nulls(self, scenario):
        event_id = uuid5(JERNERICS_NAMESPACE, "job-resource:990009")

        def capture(**overrides):
            fields = {
                "event_id": event_id,
                "recorded_at": datetime.now(UTC),
                "job_id": "990009",
                "study_name": SWEEP,
            }
            fields.update(overrides)
            return self._ingest(scenario, JobResourceEvent(**fields))

        assert capture()["accepted"] == 1
        repeat = capture(wall_time_s=3_723.0, node_list="node[01-02]")
        assert repeat["duplicates"] == 0
        assert repeat["accepted"] == 1
        again = capture(wall_time_s=3_723.0, node_list="node[01-02]")
        assert again["accepted"] == 0
        assert again["duplicates"] == 1

        body = httpx.post(
            f"{scenario.base_url}/job-resources",
            json={
                "selection": {"project": PROJECT},
                "job_ids": ["990009"],
            },
        ).json()
        record = body["records"][0]
        assert record["wall_time_s"] == pytest.approx(3_723.0)
        assert record["node_list"] == "node[01-02]"


class TestIdempotence:
    def test_full_pipeline_rerun_changes_nothing(self, scenario):
        counts_before = _row_counts(scenario.db_path)
        result = run_pipeline(
            ctx_path=str(scenario.ctx_path),
            chain_depth=0,
            tracking_dir=str(scenario.tracking_dir),
            base_url=scenario.base_url,
        )
        assert result == PipelineResult.SWEEP_COMPLETE
        replay_tracking(
            tracking_dir=scenario.tracking_dir.parent,
            base_url=scenario.base_url,
            study=SWEEP,
        )
        assert _row_counts(scenario.db_path) == counts_before
