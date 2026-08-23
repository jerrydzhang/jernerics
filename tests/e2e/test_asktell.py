"""End-to-end ask/tell flows: post-hook repair, conflicts, lineage, reconstruction."""

import json
import subprocess
import sys
from pathlib import Path
from uuid import UUID

import optuna
import pytest
from jernerics.optuna_mirror import fallback_trial_id
from jernerics.post_hook import PipelineResult, reconcile_study, run_pipeline
from jernerics.retry import RetryContext
from jernerics.retry_checker import _enqueue_retry
from jernerics.runner import run_trial
from jernerics.tracking.batch_sync import replay_tracking
from jernerics.tracking.jsonl_io import TrackingReader
from jernerics_schema import (
    FlatContext,
    SweepSnapshotEvent,
    TrialSnapshotEvent,
    TrialState,
    sweep_id_for,
)
from jernerics_server.ingest import IngestService
from jernerics_server.store import Store
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import TrialState as OptunaState

optuna.logging.set_verbosity(optuna.logging.WARNING)

_HEADER = (
    "from jernerics import trial_config, trial_tracker\n"
    "config = trial_config()\n"
    "tracker = trial_tracker()\n"
)


def _pyproject(project_dir: Path) -> None:
    (project_dir / "pyproject.toml").write_text(
        "[project]\n"
        'name = "proj"\n'
        'version = "0.1.0"\n\n'
        "[tool.jernerics.backends.slurm]\n"
        'type = "slurm"\n'
        "grace_period_s = 0\n"
    )


def _config(project_dir: Path, *, n_trials: int, objective: bool = True) -> Path:
    lines = [
        "base = {}",
        f"n_trials = {n_trials}",
        "def search_space(trial):",
        "    return {",
        "        'rate': trial.suggest_float('rate', 0.1, 0.9),",
        "        'seed': trial.suggest_int('seed', 1, 5),",
        "        'mode': trial.suggest_categorical('mode', ['a', 'b']),",
        "    }",
    ]
    if objective:
        lines.append("def objective(results):\n    return results['loss']")
    path = project_dir / "config.py"
    path.write_text("\n".join(lines) + "\n")
    return path


def _ctx(tmp_path: Path, storage_url: str, tracking_dir: Path, n_trials: int) -> Path:
    project_dir = tmp_path / "proj"
    project_dir.mkdir(exist_ok=True)
    _pyproject(project_dir)
    _config(project_dir, n_trials=n_trials)
    ctx = RetryContext(
        study_name="sweep",
        backend_name="slurm",
        trial_relpath="trial.py",
        config_relpath="config.py",
        storage_path=storage_url,
        tracking_dir=str(tracking_dir),
        project_dir=str(project_dir),
        project_name="proj",
        host_home=str(tmp_path),
    )
    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(ctx.to_json())
    return ctx_path


def _make_study(storage_url: str) -> optuna.study.Study:
    return optuna.create_study(
        study_name="sweep",
        storage=JournalStorage(JournalFileBackend(storage_url)),
    )


def _snapshots(events_path: Path) -> list[TrialSnapshotEvent]:
    with TrackingReader(events_path) as reader:
        return [event for event in reader if event.tag == "trial_snapshot"]


def _trial_rows(db_path: Path) -> dict[int, dict]:
    with Store(db_path) as store:
        _, rows_ = store.query(
            "SELECT trial_id, number, state, objective, retry_of_trial_id, "
            "retry_root_trial_id, retry_index FROM trials"
        )
        by_number = {row[1]: row for row in rows_}
        _, params = store.query(
            "SELECT trial_id, key, value_json FROM trial_params WHERE kind = 'sampled'"
        )
    params_by_trial: dict[str, dict[str, object]] = {}
    for trial_id, key, value_json in params:
        params_by_trial.setdefault(trial_id, {})[key] = json.loads(value_json)
    return {
        number: {
            "trial_id": UUID(row[0]),
            "state": row[2],
            "objective": row[3],
            "retry_of": UUID(row[4]) if row[4] else None,
            "retry_root": UUID(row[5]) if row[5] else None,
            "retry_index": row[6],
            "params": params_by_trial.get(row[0], {}),
        }
        for number, row in by_number.items()
    }


class TestPostHookRepair:
    def test_unshipped_trials_reconstructed_and_reconcile_is_idempotent(
        self, tmp_path, http_server
    ):
        base_url, db_path = http_server
        storage_url = str(tmp_path / "sweep.journal")
        _make_study(storage_url)
        tracking_dir = tmp_path / "tracking" / "sweep"
        tracking_dir.mkdir(parents=True)
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(
            _HEADER
            + "import os, sys\n"
            + "if os.environ['JERNERICS_TRIAL_NUMBER'] == '1':\n"
            + "    sys.exit(3)\n"
            + 'tracker.finish({"loss": 0.42})\n'
        )
        config_file = _config(tmp_path, n_trials=1)
        ctx_path = _ctx(tmp_path, storage_url, tracking_dir, n_trials=1)

        def _run_trial():
            run_trial(
                trial_file=str(trial_file),
                config_file=str(config_file),
                study_name="sweep",
                storage_url=storage_url,
                tracking_dir=str(tracking_dir),
                project_name="proj",
            )

        _run_trial()
        with pytest.raises(SystemExit):
            _run_trial()

        assert not (tracking_dir / "submission").exists()
        ctx = RetryContext.from_json(ctx_path.read_text())
        first = reconcile_study(ctx, tracking_dir)
        assert first is not None
        first_bytes = first.read_bytes()

        result = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tracking_dir),
            base_url=base_url,
        )
        assert result == PipelineResult.SWEEP_COMPLETE

        rows_ = _trial_rows(db_path)
        assert set(rows_) == {0, 1}
        sweep_id = sweep_id_for("proj", "sweep")
        study = optuna.load_study(
            study_name="sweep",
            storage=JournalStorage(JournalFileBackend(storage_url)),
        )
        expected_states = {
            OptunaState.COMPLETE: "completed",
            OptunaState.FAIL: "failed",
        }
        for frozen in study.trials:
            row = rows_[frozen.number]
            assert row["state"] == expected_states[frozen.state]
            assert row["params"] == frozen.params
            assert row["trial_id"] == UUID(frozen.user_attrs["jernerics_trial_id"])
            if frozen.state == OptunaState.COMPLETE:
                assert row["objective"] == pytest.approx(frozen.value)
            else:
                assert row["objective"] is None
        with Store(db_path) as store:
            objective, distributions_json, attrs_json = store.query(
                "SELECT objective, distributions_json, attrs_json FROM trials "
                "WHERE number = 0"
            )[1][0]
        assert objective == pytest.approx(0.42)
        distributions = json.loads(distributions_json)
        assert set(distributions) == {"rate", "seed", "mode"}
        for name, kinds in {
            "rate": "FloatDistribution",
            "seed": "IntDistribution",
            "mode": "CategoricalDistribution",
        }.items():
            assert kinds in distributions[name]
            json.loads(distributions[name])
        assert "jernerics_trial_id" in attrs_json
        assert fallback_trial_id(sweep_id, 0) not in {rows_[0]["trial_id"]}

        second = reconcile_study(ctx, tracking_dir)
        assert second.read_bytes() == first_bytes
        assert (
            run_pipeline(
                ctx_path=str(ctx_path),
                chain_depth=0,
                tracking_dir=str(tracking_dir),
                base_url=base_url,
            )
            == PipelineResult.SWEEP_COMPLETE
        )

        assert _trial_rows(db_path).keys() == {0, 1}
        with Store(db_path) as store:
            assert store.query("SELECT COUNT(*) FROM reconciliation_conflicts")[1] == [
                (0,)
            ]

    def test_fallback_identity_is_stable_when_live_snapshot_missing(
        self, tmp_path, http_server
    ):
        base_url, db_path = http_server
        storage_url = str(tmp_path / "sweep.journal")
        study = _make_study(storage_url)
        trial = study.ask()
        trial.suggest_float("rate", 0.1, 0.9)
        study.tell(trial, 0.7)
        tracking_dir = tmp_path / "tracking" / "sweep"
        ctx_path = _ctx(tmp_path, storage_url, tracking_dir, n_trials=1)

        ctx = RetryContext.from_json(ctx_path.read_text())
        reconcile_study(ctx, tracking_dir)
        replay_tracking(tracking_dir=tracking_dir.parent, base_url=base_url)

        expected_id = fallback_trial_id(sweep_id_for("proj", "sweep"), 0)
        rows_ = _trial_rows(db_path)
        assert rows_[0]["trial_id"] == expected_id
        assert rows_[0]["state"] == "completed"
        assert rows_[0]["objective"] == pytest.approx(0.7)


class TestTerminalConflict:
    def _seed_server_completed(
        self, db_path: Path, trial_id: UUID, params: dict
    ) -> None:
        sweep_id = sweep_id_for("proj", "sweep")
        with Store(db_path) as store:
            IngestService(store).apply(
                _request(
                    [
                        SweepSnapshotEvent(
                            event_id=UUID(int=1),
                            recorded_at=_epoch(),
                            project="proj",
                            sweep_id=sweep_id,
                            name="sweep",
                            state="running",
                        ),
                        TrialSnapshotEvent(
                            event_id=UUID(int=2),
                            recorded_at=_epoch(),
                            trial_id=trial_id,
                            sweep_id=sweep_id,
                            number=0,
                            retry_root_trial_id=trial_id,
                            state=TrialState.COMPLETED,
                            params=FlatContext(params),
                            objective=0.9,
                        ),
                    ]
                )
            )

    def test_conflicting_journal_state_fails_visibly_and_leaves_server_state(
        self, tmp_path, http_server, capsys
    ):
        base_url, db_path = http_server
        storage_url = str(tmp_path / "sweep.journal")
        study = _make_study(storage_url)
        conflicting = study.ask()
        conflicting.suggest_float("rate", 0.1, 0.9)
        live_id = UUID(int=42)
        conflicting.set_user_attr("jernerics_trial_id", str(live_id))
        study.tell(conflicting, state=OptunaState.FAIL)
        closer = study.ask()
        closer.suggest_float("rate", 0.1, 0.9)
        study.tell(closer, 0.3)
        self._seed_server_completed(db_path, live_id, dict(conflicting.params))

        tracking_dir = tmp_path / "tracking" / "sweep"
        ctx_path = _ctx(tmp_path, storage_url, tracking_dir, n_trials=1)

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "jernerics.post_hook",
                "--context",
                str(ctx_path),
                "--chain-depth",
                "0",
                "--tracking-dir",
                str(tracking_dir),
                "--server-addr",
                base_url,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        assert completed.returncode == 1
        assert "reconciliation conflict" in completed.stderr
        assert "optimizer_terminal_state" in completed.stderr
        rows_ = _trial_rows(db_path)
        assert rows_[0]["state"] == "completed"
        assert rows_[0]["objective"] == pytest.approx(0.9)
        assert rows_[1]["state"] == "completed"
        with Store(db_path) as store:
            assert store.query("SELECT trial_id, kind FROM reconciliation_conflicts")[
                1
            ] == [(str(live_id), "optimizer_terminal_state")]


class TestRetryChainEndToEnd:
    def test_two_generations_of_lineage_across_runner_and_checker(self, tmp_path):
        storage_url = str(tmp_path / "sweep.journal")
        study = _make_study(storage_url)
        tracking_dir = tmp_path / "tracking" / "sweep"
        tracking_dir.mkdir(parents=True)
        trial_file = tmp_path / "trial.py"
        trial_file.write_text(_HEADER + 'tracker.finish({"loss": 0.42})\n')
        config_file = _config(tmp_path, n_trials=1)

        stale = study.ask()
        stale.suggest_float("rate", 0.25, 0.25)
        stale.suggest_int("seed", 2, 2)
        stale.suggest_categorical("mode", ["a", "b"])
        root_id = UUID(int=100)
        stale.set_user_attr("jernerics_trial_id", str(root_id))

        sweep_id = sweep_id_for("proj", "sweep")
        _enqueue_retry(study, 0, sweep_id=sweep_id, submission_dir=tracking_dir / "sub")
        assert study.trials[1].user_attrs["retry_index"] == 1

        first_retry = study.ask()
        assert first_retry.user_attrs["retry_index"] == 1
        first_retry_id = UUID(int=101)
        first_retry.set_user_attr("jernerics_trial_id", str(first_retry_id))
        first_retry.suggest_float("rate", 0.25, 0.25)
        first_retry.suggest_int("seed", 2, 2)
        first_retry.suggest_categorical("mode", ["a", "b"])

        _enqueue_retry(study, 1, sweep_id=sweep_id, submission_dir=tracking_dir / "sub")
        second = study.trials[2]
        assert second.user_attrs["retry_of"] == 1
        assert second.user_attrs["retry_root"] == 0
        assert second.user_attrs["retry_index"] == 2
        assert UUID(second.user_attrs["retry_of_trial_id"]) == first_retry_id
        assert UUID(second.user_attrs["retry_root_trial_id"]) == root_id

        run_trial(
            trial_file=str(trial_file),
            config_file=str(config_file),
            study_name="sweep",
            storage_url=storage_url,
            tracking_dir=str(tracking_dir),
            project_name="proj",
        )
        second_snapshots = _snapshots(tracking_dir / "events" / "2.jsonl")
        for snapshot in second_snapshots:
            assert snapshot.retry_of_trial_id == first_retry_id
            assert snapshot.retry_root_trial_id == root_id
            assert snapshot.retry_index == 2

        checker_snapshots = _snapshots(tracking_dir / "sub" / "checker.jsonl")
        assert [(s.number, s.state.value) for s in checker_snapshots] == [
            (0, "failed"),
            (1, "failed"),
        ]
        assert checker_snapshots[0].trial_id == root_id
        assert checker_snapshots[1].trial_id == first_retry_id
        assert checker_snapshots[1].retry_index == 1

        study_after = optuna.load_study(
            study_name="sweep", storage=JournalStorage(JournalFileBackend(storage_url))
        )
        assert [t.state for t in study_after.trials] == [
            OptunaState.FAIL,
            OptunaState.FAIL,
            OptunaState.COMPLETE,
        ]


def _epoch():
    from datetime import UTC, datetime

    return datetime(1970, 1, 1, tzinfo=UTC)


def _request(events):
    from jernerics_schema import PROTOCOL_VERSION, IngestRequest

    return IngestRequest(protocol_version=PROTOCOL_VERSION, events=events)
