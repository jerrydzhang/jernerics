import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4, uuid5

import pytest
from jernerics.backend.slurm.sacct import SacctResult
from jernerics.post_hook import (
    PipelineResult,
    ReconciliationConflictError,
    main,
    run_pipeline,
)
from jernerics.retry import RetryContext
from jernerics.tracking.batch_sync import ReplayResult
from jernerics.tracking.jsonl_io import scan_events
from jernerics_schema import (
    ExecutionEndEvent,
    ExecutionStartEvent,
    ExecutionOutcome,
    FailureKind,
    JERNERICS_NAMESPACE,
    JobResourceEvent,
    JobSnapshotEvent,
    SubmissionState,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
)


def _write_ctx(tmp_path, study_name="mystudy"):
    ctx = RetryContext(
        study_name=study_name,
        backend_name="slurm",
        trial_relpath="trial.py",
        config_relpath="config.py",
        project_name="myproject",
    )
    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(ctx.to_json())
    return ctx_path


class TestRunPipeline:
    @patch("jernerics.post_hook.run_checker")
    def test_returns_retry_submitted_when_retries_happen(self, mock_run_checker):
        mock_run_checker.side_effect = None  # ran without exception = submitted retry

        result = run_pipeline(
            ctx_path="/cache/retry/ctx.json",
            chain_depth=2,
            tracking_dir="/cache/tracking/mystudy",
        )

        assert result == PipelineResult.RETRY_SUBMITTED

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_returns_sweep_complete_when_no_retries(
        self, mock_run_checker, mock_replay, tmp_path
    ):
        mock_run_checker.return_value = None  # sweep complete

        result = run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_not_called()

    @patch("jernerics.post_hook.run_checker")
    def test_passes_args_to_run_checker(self, mock_run_checker):
        mock_run_checker.return_value = False
        run_pipeline(
            ctx_path="/ctx.json",
            chain_depth=3,
            tracking_dir="/cache/tracking/s",
        )

        mock_run_checker.assert_called_once_with(ctx_path="/ctx.json", chain_depth=3)

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_replays_tracking_on_sweep_complete_with_server(
        self, mock_run_checker, mock_replay, tmp_path
    ):
        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult()
        ctx_path = _write_ctx(tmp_path)

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
            base_url="http://localhost:8000",
            api_key="secret",
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_called_once()
        kwargs = mock_replay.mock_calls[0].kwargs
        assert kwargs["base_url"] == "http://localhost:8000"
        assert kwargs["api_key"] == "secret"
        assert kwargs["study"] == "mystudy"
        assert kwargs["tracking_dir"] == tmp_path / "tracking"

    @patch("jernerics.post_hook.capture_job_resources")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_captures_resources_before_replay_ships_submission_files(
        self, mock_run_checker, mock_replay, mock_capture, tmp_path
    ):
        from jernerics.post_hook import _sweep_job_ids

        mock_run_checker.return_value = None
        order = []
        discovered = []
        submission_dir = tmp_path / "tracking" / "mystudy" / "submission"
        submission_dir.mkdir(parents=True)
        submission_id = uuid4()
        (submission_dir / "deploy.jsonl").write_text(
            JobSnapshotEvent(
                event_id=uuid4(),
                recorded_at=datetime.now(UTC),
                job_id=uuid4(),
                submission_id=submission_id,
                scheduler_job_id="990001",
                role="trials",
                state=SubmissionState.SUBMITTED,
            ).model_dump_json()
            + "\n"
        )

        def capture(tracking_dir, study_name, base_url, api_key):
            order.append("capture")
            discovered.append(_sweep_job_ids(Path(tracking_dir), study_name))

        def replay(**kwargs):
            order.append("replay")
            return ReplayResult()

        mock_capture.side_effect = capture
        mock_replay.side_effect = replay

        result = run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
            base_url="http://localhost:8000",
            api_key="secret",
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        assert order == ["capture", "replay"]
        assert discovered == [{"990001": str(submission_id)}]

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_skips_replay_when_no_server(self, mock_run_checker, mock_replay, tmp_path):
        mock_run_checker.return_value = None

        result = run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_replay.assert_not_called()

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_skips_replay_on_retry_submitted(
        self, mock_run_checker, mock_replay, tmp_path
    ):
        mock_run_checker.side_effect = None  # submitted retries

        result = run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
            base_url="http://localhost:8000",
        )

        assert result == PipelineResult.RETRY_SUBMITTED
        mock_replay.assert_not_called()


class TestBlobSweep:
    @patch("jernerics.tracking.blob_uploader.upload_pending_blobs")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_sweeps_all_study_manifests_after_replay(
        self, mock_run_checker, mock_replay, mock_upload, tmp_path
    ):
        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult()
        tracking_root = tmp_path / "tracking"
        study_manifest = tracking_root / "mystudy" / "artifacts" / "0.manifest"
        other_manifest = tracking_root / "otherstudy" / "artifacts" / "3.manifest"
        for path in (study_manifest, other_manifest):
            path.parent.mkdir(parents=True)
            path.write_text('{"artifact_id": "%s"}\n' % ("a" * 32))

        run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tracking_root / "mystudy"),
            base_url="http://localhost:8000",
            api_key="sekret",
        )

        mock_upload.assert_called_once_with(
            "http://localhost:8000",
            "sekret",
            [study_manifest, other_manifest],
        )

    @patch("jernerics.tracking.blob_uploader.upload_pending_blobs")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_conflicts_skip_the_blob_sweep(
        self, mock_run_checker, mock_replay, mock_upload, tmp_path
    ):
        from jernerics_schema import ConflictRecord

        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult(
            conflicts=[ConflictRecord(trial_id=uuid4(), kind="k", detail="d")]
        )

        with pytest.raises(ReconciliationConflictError):
            run_pipeline(
                ctx_path=str(_write_ctx(tmp_path)),
                chain_depth=0,
                tracking_dir=str(tmp_path / "tracking" / "mystudy"),
                base_url="http://localhost:8000",
            )

        mock_upload.assert_not_called()

    @patch("jernerics.tracking.blob_uploader.upload_pending_blobs")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_no_manifests_means_no_sweep(
        self, mock_run_checker, mock_replay, mock_upload, tmp_path
    ):
        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult()

        run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
            base_url="http://localhost:8000",
        )

        mock_upload.assert_not_called()


class TestSchedulerTaskLogs:
    @patch("jernerics.tracking.blob_uploader.upload_pending_blobs")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_unmappable_task_logs_skip_with_stderr_note(
        self, mock_run_checker, mock_replay, mock_upload, tmp_path, capsys
    ):
        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult()
        cache = tmp_path
        tracking_dir = cache / "tracking" / "mystudy"
        tracking_dir.mkdir(parents=True)
        logs_dir = cache / "logs"
        logs_dir.mkdir()
        (logs_dir / "123_1.out").write_text("task log")
        (cache / "jobs").mkdir()
        (cache / "jobs" / "123.json").write_text(
            json.dumps(
                {
                    "job_id": "123",
                    "remote_dir": str(tmp_path),
                    "n_trials": 1,
                    "output_pattern": f"{logs_dir}/%A_%a.out",
                    "error_pattern": f"{logs_dir}/%A_%a.err",
                }
            )
        )

        run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tracking_dir),
            base_url="http://localhost:8000",
        )

        err = capsys.readouterr().err
        assert "scheduler task log" in err
        assert "no trial association" in err
        assert (logs_dir / "123_1.out").read_text() == "task log"

    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_no_job_metadata_means_no_note(
        self, mock_run_checker, mock_replay, tmp_path, capsys
    ):
        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult()

        run_pipeline(
            ctx_path=str(_write_ctx(tmp_path)),
            chain_depth=0,
            tracking_dir=str(tmp_path / "tracking" / "mystudy"),
            base_url="http://localhost:8000",
        )

        assert "scheduler task log" not in capsys.readouterr().err


class TestReconcileStudy:
    def _journal(self, tmp_path, name="s"):
        import optuna
        from optuna.storages.journal import JournalFileBackend, JournalStorage

        storage_url = str(tmp_path / f"{name}.journal")
        study = optuna.create_study(
            study_name=name,
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        return study, storage_url

    def _ctx(self, tmp_path, storage_url, study_name="s", project_name="proj"):
        return RetryContext(
            study_name=study_name,
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            storage_path=storage_url,
            project_name=project_name,
        )

    def test_missing_journal_returns_empty(self, tmp_path):
        from jernerics.post_hook import reconcile_study

        ctx = self._ctx(tmp_path, str(tmp_path / "absent.journal"))

        assert reconcile_study(ctx, tmp_path / "tracking" / "s") == []

    def test_reconciles_every_frozen_trial_with_deterministic_ids(self, tmp_path):
        from jernerics.optuna_mirror import fallback_trial_id
        from jernerics.post_hook import reconcile_study
        from jernerics.tracking.jsonl_io import TrackingReader
        from jernerics_schema import sweep_id_for
        from optuna.trial import TrialState as OptunaState

        study, storage_url = self._journal(tmp_path)
        live = study.ask()
        live.suggest_float("lr", 0.1, 0.1)
        live.set_user_attr("jernerics_trial_id", str(_LIVE_ID))
        study.tell(live, 0.42)
        orphan = study.ask()
        orphan.suggest_float("lr", 0.2, 0.2)
        study.tell(orphan, state=OptunaState.FAIL)

        tracking_dir = tmp_path / "tracking" / "s"
        paths = reconcile_study(self._ctx(tmp_path, storage_url), tracking_dir)

        assert paths == [tracking_dir / "submission" / "reconcile.jsonl"]
        path = paths[0]
        sweep_events = [
            event
            for event in TrackingReader(
                tracking_dir / "submission" / "reconcile-sweep.jsonl"
            )
            if isinstance(event, SweepSnapshotEvent)
        ]
        assert len(sweep_events) == 1
        assert sweep_events[0].sweep_id == sweep_id_for("proj", "s")
        events = [
            event
            for event in TrackingReader(path)
            if isinstance(event, TrialSnapshotEvent)
        ]
        sweep_id = sweep_id_for("proj", "s")
        assert len(events) == 2
        live_snapshot, orphan_snapshot = events
        assert live_snapshot.trial_id == _LIVE_ID
        assert live_snapshot.state == TrialState.COMPLETED
        assert live_snapshot.objective == pytest.approx(0.42)
        assert live_snapshot.params.root["lr"] == pytest.approx(0.1)
        assert live_snapshot.distributions is not None
        lr_distribution = live_snapshot.distributions.root["lr"]
        assert isinstance(lr_distribution, str)
        assert '"FloatDistribution"' in lr_distribution
        assert live_snapshot.attrs is not None
        assert live_snapshot.attrs.root["jernerics_trial_id"] == str(_LIVE_ID)
        assert orphan_snapshot.trial_id == fallback_trial_id(sweep_id, 1)
        assert orphan_snapshot.state == TrialState.FAILED
        assert orphan_snapshot.objective is None

    def test_repeated_reconciles_are_byte_identical(self, tmp_path):
        from jernerics.post_hook import reconcile_study
        from optuna.trial import TrialState

        study, storage_url = self._journal(tmp_path)
        trial = study.ask()
        trial.suggest_float("lr", 0.1, 0.1)
        study.tell(trial, 0.42)
        other = study.ask()
        study.tell(other, state=TrialState.FAIL)

        tracking_dir = tmp_path / "tracking" / "s"
        ctx = self._ctx(tmp_path, storage_url)
        first = reconcile_study(ctx, tracking_dir)
        assert first
        first_bytes = {path.name: path.read_bytes() for path in first}

        second = reconcile_study(ctx, tracking_dir)

        assert [path.name for path in second] == list(first_bytes)
        for path in second:
            assert path.read_bytes() == first_bytes[path.name]


RECONCILE_T0 = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)


class TestReconcileDeadExecutions:
    def _journal(self, tmp_path, name="s"):
        import optuna
        from optuna.storages.journal import JournalFileBackend, JournalStorage

        storage_url = str(tmp_path / f"{name}.journal")
        study = optuna.create_study(
            study_name=name,
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        return study, storage_url

    def _ctx(self, tmp_path, storage_url, study_name="s", project_name="proj"):
        return RetryContext(
            study_name=study_name,
            backend_name="slurm",
            trial_relpath="trial.py",
            config_relpath="config.py",
            storage_path=storage_url,
            project_name=project_name,
        )

    def _ctx_file(self, tmp_path, storage_url):
        ctx_path = tmp_path / "ctx.json"
        ctx_path.write_text(self._ctx(tmp_path, storage_url).to_json())
        return ctx_path

    def _fail_trial(self, study, trial_id):
        from optuna.trial import TrialState

        trial = study.ask()
        trial.set_user_attr("jernerics_trial_id", str(trial_id))
        study.tell(trial, state=TrialState.FAIL)
        return trial

    def _write_start(self, tracking_dir, number, execution_id, trial_id):
        events_dir = tracking_dir / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        with (events_dir / f"{number}.jsonl").open("a") as events:
            events.write(
                ExecutionStartEvent(
                    event_id=uuid4(),
                    recorded_at=RECONCILE_T0,
                    execution_id=execution_id,
                    trial_id=trial_id,
                    hostname="node1",
                    started_at=RECONCILE_T0,
                ).model_dump_json()
                + "\n"
            )

    def _write_end(self, tracking_dir, number, execution_id):
        with (tracking_dir / "events" / f"{number}.jsonl").open("a") as events:
            events.write(
                ExecutionEndEvent(
                    event_id=uuid4(),
                    recorded_at=RECONCILE_T0,
                    execution_id=execution_id,
                    ended_at=RECONCILE_T0,
                    outcome=ExecutionOutcome.SUCCESS,
                    exit_code=0,
                ).model_dump_json()
                + "\n"
            )

    def _ends(self, tracking_dir):
        from jernerics.tracking.jsonl_io import TrackingReader

        path = tracking_dir / "submission" / "reconcile-executions.jsonl"
        if not path.exists():
            return []
        return [
            event
            for event in TrackingReader(path)
            if isinstance(event, ExecutionEndEvent)
        ]

    def test_dead_execution_emits_one_deterministic_end(self, tmp_path):
        from jernerics.post_hook import reconcile_study

        study, storage_url = self._journal(tmp_path)
        self._fail_trial(study, _LIVE_ID)
        tracking_dir = tmp_path / "tracking" / "s"
        execution_id = uuid4()
        self._write_start(tracking_dir, 0, execution_id, _LIVE_ID)

        paths = reconcile_study(self._ctx(tmp_path, storage_url), tracking_dir)

        assert paths == [
            tracking_dir / "submission" / "reconcile.jsonl",
            tracking_dir / "submission" / "reconcile-executions.jsonl",
        ]
        ends = self._ends(tracking_dir)
        assert len(ends) == 1
        end = ends[0]
        assert end.event_id == uuid5(
            JERNERICS_NAMESPACE, f"reconcile-end:{execution_id}"
        )
        assert end.execution_id == execution_id
        assert end.outcome == ExecutionOutcome.FAILURE
        assert end.exit_code is None
        assert end.failure_kind == FailureKind.STALE_HEARTBEAT
        assert end.failure_summary is not None
        assert len(end.failure_summary) <= 2000
        assert "reconciled" in end.failure_summary
        assert end.ended_at.timestamp() == study.trials[0].datetime_complete.timestamp()

    def test_repeated_reconcile_ends_are_byte_identical(self, tmp_path):
        from jernerics.post_hook import reconcile_study

        study, storage_url = self._journal(tmp_path)
        self._fail_trial(study, _LIVE_ID)
        tracking_dir = tmp_path / "tracking" / "s"
        self._write_start(tracking_dir, 0, uuid4(), _LIVE_ID)
        ctx = self._ctx(tmp_path, storage_url)

        first = reconcile_study(ctx, tracking_dir)
        first_bytes = {path.name: path.read_bytes() for path in first}
        second = reconcile_study(ctx, tracking_dir)

        assert [path.name for path in second] == list(first_bytes)
        for path in second:
            assert path.read_bytes() == first_bytes[path.name]

    def test_running_trial_with_heartbeat_gets_no_end(self, tmp_path):
        from jernerics.post_hook import reconcile_study

        study, storage_url = self._journal(tmp_path)

        trial = study.ask()
        trial.set_user_attr("jernerics_trial_id", str(_LIVE_ID))
        tracking_dir = tmp_path / "tracking" / "s"
        heartbeats = tracking_dir / "heartbeats"
        heartbeats.mkdir(parents=True)
        (heartbeats / "0.heartbeat").touch()
        self._write_start(tracking_dir, 0, uuid4(), _LIVE_ID)

        paths = reconcile_study(self._ctx(tmp_path, storage_url), tracking_dir)

        assert paths == [tracking_dir / "submission" / "reconcile.jsonl"]
        assert self._ends(tracking_dir) == []

    def test_locally_ended_execution_gets_no_end(self, tmp_path):
        from jernerics.post_hook import reconcile_study
        from optuna.trial import TrialState

        study, storage_url = self._journal(tmp_path)
        trial = study.ask()
        trial.set_user_attr("jernerics_trial_id", str(_LIVE_ID))
        study.tell(trial, 0.5)
        assert study.trials[0].state == TrialState.COMPLETE
        tracking_dir = tmp_path / "tracking" / "s"
        execution_id = uuid4()
        self._write_start(tracking_dir, 0, execution_id, _LIVE_ID)
        self._write_end(tracking_dir, 0, execution_id)

        paths = reconcile_study(self._ctx(tmp_path, storage_url), tracking_dir)

        assert paths == [tracking_dir / "submission" / "reconcile.jsonl"]
        assert self._ends(tracking_dir) == []

    @patch("jernerics.post_hook.ship_events_file")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_retry_submitted_still_ships_dead_execution_ends(
        self, mock_run_checker, mock_replay, mock_ship, tmp_path
    ):
        from jernerics.post_hook import run_pipeline

        mock_run_checker.return_value = True
        study, storage_url = self._journal(tmp_path)
        self._fail_trial(study, _LIVE_ID)
        tracking_dir = tmp_path / "tracking" / "s"
        execution_id = uuid4()
        self._write_start(tracking_dir, 0, execution_id, _LIVE_ID)
        ctx_path = self._ctx_file(tmp_path, storage_url)

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tracking_dir),
            base_url="http://localhost:8000",
            api_key="secret",
        )

        assert result == PipelineResult.RETRY_SUBMITTED
        mock_replay.assert_not_called()
        shipped = [call.args[0] for call in mock_ship.call_args_list]
        executions_path = tracking_dir / "submission" / "reconcile-executions.jsonl"
        assert executions_path in shipped
        ends = self._ends(tracking_dir)
        assert len(ends) == 1
        assert ends[0].event_id == uuid5(
            JERNERICS_NAMESPACE, f"reconcile-end:{execution_id}"
        )
        assert ends[0].outcome == ExecutionOutcome.FAILURE
        assert ends[0].failure_kind == FailureKind.STALE_HEARTBEAT


class TestCaptureJobResources:
    def _write_submission_events(self, tracking_dir, submission_id, scheduler_id):
        from jernerics_schema import SubmissionSnapshotEvent, sweep_id_for

        submission_dir = tracking_dir / "submission"
        submission_dir.mkdir(parents=True)
        now = datetime.now(UTC)
        sweep_id = sweep_id_for("proj", "mystudy")
        events = [
            SweepSnapshotEvent(
                event_id=uuid4(),
                recorded_at=now,
                project="proj",
                sweep_id=sweep_id,
                name="mystudy",
                state="running",
            ),
            SubmissionSnapshotEvent(
                event_id=uuid4(),
                recorded_at=now,
                submission_id=submission_id,
                sweep_id=sweep_id,
                backend="slurm",
                state=SubmissionState.SUBMITTED,
            ),
            JobSnapshotEvent(
                event_id=uuid4(),
                recorded_at=now,
                job_id=uuid4(),
                submission_id=submission_id,
                scheduler_job_id=scheduler_id,
                role="trials",
                state=SubmissionState.SUBMITTED,
            ),
        ]
        (submission_dir / "deploy.jsonl").write_text(
            "".join(event.model_dump_json() + "\n" for event in events)
        )

    def _snapshot(self, job_id):
        from jernerics.backend.slurm.sacct import JobResourceSnapshot

        return JobResourceSnapshot(
            job_id=job_id,
            state="COMPLETED",
            exit_code="0:0",
            wall_time_s=600.0,
            cpu_time_s=2_400.0,
            cpu_pct=400.0,
            max_rss_mb=512.0,
            ave_rss_mb=256.0,
            alloc_cpus=4,
            req_mem="16G",
            alloc_tres="cpu=4,mem=16G",
            node_list="node01",
        )

    def test_ships_one_event_per_job_with_submission_linkage(self, tmp_path):
        from jernerics.post_hook import capture_job_resources
        from jernerics.tracking.jsonl_io import scan_events

        tracking_dir = tmp_path / "tracking" / "mystudy"
        submission_id = uuid4()
        self._write_submission_events(tracking_dir, submission_id, "990001")
        with (
            patch("jernerics.post_hook.fetch_job_resources") as fetch,
            patch("jernerics.post_hook.ship_events_file") as ship,
        ):
            fetch.return_value = SacctResult(self._snapshot("990001"), None)
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", "key")

        fetch.assert_called_once_with("990001")
        ship.assert_called_once()
        path, base_url = ship.call_args.args[:2]
        assert base_url == "http://srv"
        assert ship.call_args.args[2] == "key"
        assert path.parent == tracking_dir / "submission"
        assert path.name.startswith("resources-")
        events, _ = scan_events(path, 0)
        assert len(events) == 1
        event = events[0][0]
        assert isinstance(event, JobResourceEvent)
        assert event.job_id == "990001"
        assert event.study_name == "mystudy"
        assert event.submission_id == str(submission_id)
        assert event.wall_time_s == pytest.approx(600.0)

    def test_falls_back_to_job_meta_without_submission_events(self, tmp_path):
        from jernerics.post_hook import capture_job_resources

        tracking_dir = tmp_path / "tracking" / "mystudy"
        tracking_dir.mkdir(parents=True)
        jobs_dir = tmp_path / "jobs"
        jobs_dir.mkdir()
        (jobs_dir / "990002.json").write_text(
            json.dumps({"job_id": "990002", "study_name": "mystudy"})
        )
        (jobs_dir / "880003.json").write_text(
            json.dumps({"job_id": "880003", "study_name": "otherstudy"})
        )
        with (
            patch("jernerics.post_hook.fetch_job_resources") as fetch,
            patch("jernerics.post_hook.ship_events_file") as ship,
        ):
            fetch.return_value = SacctResult(self._snapshot("990002"), None)
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        fetch.assert_called_once_with("990002")
        ship.assert_called_once()
        events, _ = scan_events(ship.call_args.args[0], 0)
        event = events[0][0]
        assert isinstance(event, JobResourceEvent)
        assert event.submission_id is None
        assert event.study_name == "mystudy"

    def test_failed_job_logs_one_line_and_ships_the_rest(self, tmp_path, capsys):
        from jernerics.post_hook import capture_job_resources

        tracking_dir = tmp_path / "tracking" / "mystudy"
        self._write_submission_events(tracking_dir, uuid4(), "990004")
        (tracking_dir / "submission" / "retry.jsonl").write_text(
            JobSnapshotEvent(
                event_id=uuid4(),
                recorded_at=datetime.now(UTC),
                job_id=uuid4(),
                submission_id=uuid4(),
                scheduler_job_id="990005",
                role="trials",
                state=SubmissionState.SUBMITTED,
            ).model_dump_json()
            + "\n"
        )
        snapshots = {"990004": self._snapshot("990004"), "990005": None}

        def fake_fetch(job_id):
            if snapshots[job_id] is None:
                return SacctResult(None, f"sacct for job {job_id} timed out")
            return SacctResult(snapshots[job_id], None)

        with (
            patch("jernerics.post_hook.fetch_job_resources", side_effect=fake_fetch),
            patch("jernerics.post_hook.ship_events_file") as ship,
        ):
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        err = capsys.readouterr().err
        assert "990005" in err and "timed out" in err
        ship.assert_called_once()
        resources = [
            event
            for event, _ in scan_events(ship.call_args.args[0], 0)[0]
            if isinstance(event, JobResourceEvent)
        ]
        assert [event.job_id for event in resources] == ["990004"]

    def test_no_job_ids_logs_and_skips(self, tmp_path, capsys):
        from jernerics.post_hook import capture_job_resources

        tracking_dir = tmp_path / "tracking" / "mystudy"
        tracking_dir.mkdir(parents=True)
        with patch("jernerics.post_hook.ship_events_file") as ship:
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        assert "no job ids" in capsys.readouterr().err
        ship.assert_not_called()

    def test_capture_never_raises(self, tmp_path, capsys):
        from jernerics.post_hook import capture_job_resources

        tracking_dir = tmp_path / "tracking" / "mystudy"
        self._write_submission_events(tracking_dir, uuid4(), "990006")
        with (
            patch(
                "jernerics.post_hook.fetch_job_resources",
                side_effect=RuntimeError("boom"),
            ),
            patch("jernerics.post_hook.ship_events_file") as ship,
        ):
            capture_job_resources(str(tracking_dir), "mystudy", "http://srv", None)

        assert "resource capture failed" in capsys.readouterr().err
        ship.assert_not_called()

    @patch("jernerics.post_hook.capture_job_resources")
    @patch("jernerics.tracking.blob_uploader.upload_pending_blobs")
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_pipeline_captures_after_replay_on_sweep_complete(
        self, mock_run_checker, mock_replay, mock_upload, mock_capture, tmp_path
    ):
        mock_run_checker.return_value = None
        mock_replay.return_value = ReplayResult()
        ctx_path = _write_ctx(tmp_path)
        tracking_dir = tmp_path / "tracking" / "mystudy"

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tracking_dir),
            base_url="http://localhost:8000",
        )

        assert result == PipelineResult.SWEEP_COMPLETE
        mock_capture.assert_called_once_with(
            str(tracking_dir), "mystudy", "http://localhost:8000", None
        )


class TestMainSchemeLessServerAddr:
    @patch("jernerics.post_hook.replay_tracking")
    @patch("jernerics.post_hook.run_checker")
    def test_scheme_less_server_addr_exits_cleanly_before_any_work(
        self, mock_run_checker, mock_replay, tmp_path, capsys
    ):
        ctx_path = _write_ctx(tmp_path)

        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--context",
                    str(ctx_path),
                    "--chain-depth",
                    "0",
                    "--tracking-dir",
                    str(tmp_path),
                    "--server-addr",
                    "atlas.taile454b.ts.net:443",
                ]
            )

        assert excinfo.value.code == 1
        mock_run_checker.assert_not_called()
        mock_replay.assert_not_called()
        err = capsys.readouterr().err
        assert "Error:" in err
        assert "JERNERICS_TRACKING_SERVER" in err
        assert "[tool.jernerics] tracking_server" in err
        assert "atlas.taile454b.ts.net:443" in err


_LIVE_ID = uuid4()
