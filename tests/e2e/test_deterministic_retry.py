"""End-to-end proof that in-runner FAILs respect the per-combo retry ledger.

jernerics-u32: on the stochastic path a FAIL falls out of ``plan_retry`` and
its slot refills with a free-sampled fresh trial. For a grid sweep that fresh
trial is a silent duplicate of a combo that was never attempted — grid dedup
broken. The deterministic path must instead retry the failed combo with
same-params lineage under ``max_retries``, then go terminal with no
un-attempted combos injected.

Both scenarios run the real pipeline pieces: runner trials over a real
journal study (real ``study.tell(FAIL)`` on child failure), the real
``run_checker`` through ``run_pipeline`` (only the scheduler submission is
stubbed — it would need sbatch), the real ledger file, and a real tracking
server for the server-side completeness assertions.
"""

import contextlib
import itertools
import socket
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

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
from jernerics.retry import RetryContext, param_key, read_ledger
from jernerics.runner import run_trial
from jernerics.tracking.batch_sync import ship_events_file
from jernerics_server.http import create_app
from jernerics_server.store import Store
from optuna.storages.journal import JournalFileBackend, JournalStorage
from optuna.trial import TrialState

optuna.logging.set_verbosity(optuna.logging.WARNING)

PROJECT = "proj"
SWEEP = "retry-e2e"
BACKEND = "slurm"
MAX_RETRIES = 2

PYPROJECT_SOURCE = f"""\
[project]
name = "{PROJECT}"
version = "0.1.0"

[tool.jernerics.backends.{BACKEND}]
type = "slurm"
grace_period_s = 0
stale_after_s = 120
fast_fail_threshold_s = 30
max_retries = {MAX_RETRIES}
"""

_DEPLOY_JOBS = (
    JobSubmission(job_id="990001", role="trials", n_trials=0),
    JobSubmission(job_id="990002", role="post_hook", n_trials=0),
)


def _start_server(
    tmp_path: Path,
) -> tuple[str, Path, uvicorn.Server, threading.Thread, Store]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db_path = tmp_path / "tracking.sqlite"
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
    return f"http://127.0.0.1:{port}", db_path, server, thread, store


def _stop_server(
    server: uvicorn.Server, thread: threading.Thread, store: Store
) -> None:
    server.should_exit = True
    thread.join(timeout=5)
    store.close()


def _sql(db_path: Path, query: str, params: list | None = None):
    with Store(db_path) as store:
        return store.query(query, params)[1]


def _scenario(
    tmp_path: Path,
    request,
    base_url: str,
    config_source: str,
    trial_source: str,
    n_trials: int,
    grid: dict[str, list] | None,
) -> SimpleNamespace:
    """Deploy, run the initial batch, and cycle the real checker to terminal.

    The scheduler submission is stubbed (no sbatch in CI); everything else —
    planning, lineage enqueue, ledger persistence, live streaming, replay,
    reconciliation — is the real code path. Each retry batch runs through the
    real runner, which pops the enqueued WAITING retries first.
    """
    project_dir = tmp_path / PROJECT
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(PYPROJECT_SOURCE)
    (project_dir / "config.py").write_text(config_source)
    (project_dir / "trial.py").write_text(trial_source)

    storage_url = str(tmp_path / "sweep.journal")
    tracking_dir = tmp_path / "tracking" / SWEEP
    tracking_dir.mkdir(parents=True)

    study = optuna.create_study(
        study_name=SWEEP,
        storage=JournalStorage(JournalFileBackend(storage_url)),
        sampler=optuna.samplers.TPESampler(seed=7),
    )
    if grid is not None:
        keys = sorted(grid)
        for combo in itertools.product(*(grid[k] for k in keys)):
            study.enqueue_trial(dict(zip(keys, combo, strict=True)))

    spec = SweepSubmission(
        trial_path=project_dir / "trial.py",
        config_path=project_dir / "config.py",
        study_name=SWEEP,
        storage_url=storage_url,
        n_trials=n_trials,
        trial_relpath="trial.py",
        config_relpath="config.py",
        project_name=PROJECT,
        git_hash="deadbeef",
        grid=grid,
    )
    write_submission_events(
        build_submission_events(spec, BACKEND, SubmitResult(submissions=_DEPLOY_JOBS)),
        LocalHost(),
        str(tracking_dir),
        "deploy.jsonl",
    )
    assert ship_events_file(tracking_dir / "submission" / "deploy.jsonl", base_url)

    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(
        RetryContext(
            study_name=SWEEP,
            backend_name=BACKEND,
            trial_relpath="trial.py",
            config_relpath="config.py",
            storage_path=storage_url,
            tracking_dir=str(tracking_dir),
            project_dir=str(project_dir),
            project_name=PROJECT,
            host_home=str(tmp_path),
            server_addr=base_url,
        ).to_json()
    )

    submitted_specs: list[SweepSubmission] = []

    def _stub_submit(sweep_spec, infra, **kwargs):
        submitted_specs.append(sweep_spec)
        return SubmitResult(submissions=[])

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("jernerics.retry_checker.submit_sweep", _stub_submit)
    request.addfinalizer(monkeypatch.undo)

    def _run_trial():
        with contextlib.suppress(SystemExit):
            run_trial(
                trial_file=str(project_dir / "trial.py"),
                config_file=str(project_dir / "config.py"),
                study_name=SWEEP,
                storage_url=storage_url,
                tracking_dir=str(tracking_dir),
                project_name=PROJECT,
                server_addr=base_url,
                heartbeat_interval_s=0.05,
            )

    for _ in range(n_trials):
        _run_trial()

    results = []
    for _ in range(10):
        outcome = run_pipeline(
            ctx_path=str(ctx_path),
            chain_depth=0,
            tracking_dir=str(tracking_dir),
            base_url=base_url,
        )
        results.append(outcome)
        if outcome == PipelineResult.SWEEP_COMPLETE:
            break
        batch = submitted_specs[-1].n_trials
        for _ in range(batch):
            _run_trial()

    final_study = optuna.load_study(
        study_name=SWEEP,
        storage=JournalStorage(JournalFileBackend(storage_url)),
    )
    return SimpleNamespace(
        study=final_study,
        results=results,
        submitted_specs=submitted_specs,
        ledger=read_ledger(tracking_dir / ".retry_ledger.json"),
    )


GRID = {"a": [1, 2], "b": [10, 20]}
DOOMED = {"a": 1, "b": 10}

GRID_CONFIG = f"""\
base = {{"note": "grid-retry-e2e"}}
grid = {GRID!r}

def objective(results):
    return results["loss"]
"""

GRID_TRIAL = """\
import sys
from jernerics import trial_config, trial_tracker

config = trial_config()
tracker = trial_tracker()
if config["a"] == 1 and config["b"] == 10:
    print("doomed combo failing", file=sys.stderr)
    raise SystemExit(1)
tracker.finish({"loss": float(config["a"]) + config["b"] / 100})
"""


def _combo_key(params: dict) -> tuple:
    return tuple(sorted(params.items()))


def _grid_combo_keys() -> set[tuple]:
    keys = sorted(GRID)
    return {
        _combo_key(dict(zip(keys, combo, strict=True)))
        for combo in itertools.product(*(GRID[k] for k in keys))
    }


@pytest.fixture(scope="module")
def grid_scenario(tmp_path_factory, request):
    tmp_path = tmp_path_factory.mktemp("grid-retry")
    base_url, db_path, server, thread, store = _start_server(tmp_path)
    request.addfinalizer(lambda: _stop_server(server, thread, store))
    scenario = _scenario(
        tmp_path,
        request,
        base_url,
        GRID_CONFIG,
        GRID_TRIAL,
        n_trials=4,
        grid=GRID,
    )
    scenario.db_path = db_path
    return scenario


class TestDeterministicGridRetry:
    def test_failed_combo_retried_exactly_max_retries_then_terminal(
        self, grid_scenario
    ):
        s = grid_scenario
        assert s.results == [
            PipelineResult.RETRY_SUBMITTED,
            PipelineResult.RETRY_SUBMITTED,
            PipelineResult.SWEEP_COMPLETE,
        ]
        assert [spec.n_trials for spec in s.submitted_specs] == [1, 1]
        assert s.ledger == {param_key(DOOMED): MAX_RETRIES}

    def test_no_free_sampled_duplicates_in_study(self, grid_scenario):
        s = grid_scenario
        trials = s.study.trials
        counts = Counter(_combo_key(t.params) for t in trials)

        assert set(counts) <= _grid_combo_keys()
        assert counts[_combo_key(DOOMED)] == 1 + MAX_RETRIES
        for combo, count in counts.items():
            if combo != _combo_key(DOOMED):
                assert count == 1
        assert len(trials) == 4 + MAX_RETRIES

    def test_retry_family_renders_generations(self, grid_scenario):
        s = grid_scenario
        trials = s.study.trials
        doomed_number = next(
            t.number for t in trials if _combo_key(t.params) == _combo_key(DOOMED)
        )
        retries = sorted(
            (t for t in trials if t.user_attrs.get("retry_of") is not None),
            key=lambda t: t.user_attrs["retry_index"],
        )
        assert [t.user_attrs["retry_index"] for t in retries] == [1, 2]
        assert all(t.user_attrs["retry_root"] == doomed_number for t in retries)
        assert retries[0].user_attrs["retry_of"] == doomed_number
        assert retries[1].user_attrs["retry_of"] == retries[0].number

        for trial in trials:
            if _combo_key(trial.params) == _combo_key(DOOMED):
                assert trial.state == TrialState.FAIL
            else:
                assert trial.state == TrialState.COMPLETE

    def test_server_shows_failures_and_terminal_counts(self, grid_scenario):
        s = grid_scenario
        states = dict(
            _sql(s.db_path, "SELECT state, COUNT(*) FROM trials GROUP BY state")
        )
        assert states == {"completed": 3, "failed": 3}

        outcomes = _sql(
            s.db_path,
            "SELECT outcome, exit_code, failure_summary FROM executions "
            "WHERE outcome = 'failure'",
        )
        assert len(outcomes) == 1 + MAX_RETRIES
        for _outcome, exit_code, summary in outcomes:
            assert exit_code == 1
            assert "exited with code 1" in summary

        live = _sql(
            s.db_path,
            "SELECT COUNT(*) FROM trials WHERE state IN ('waiting', 'running')",
        )
        assert live[0][0] == 0


STOCHASTIC_CONFIG = """\
base = {"note": "stochastic-retry-e2e"}
n_trials = 2

def search_space(trial):
    return {"rate": trial.suggest_float("rate", 0.0, 1.0)}

def objective(results):
    return results["loss"]
"""

STOCHASTIC_TRIAL = """\
import os
import sys
from jernerics import trial_config, trial_tracker

config = trial_config()
tracker = trial_tracker()
if int(os.environ["JERNERICS_TRIAL_NUMBER"]) == 0:
    print("first trial failing", file=sys.stderr)
    raise SystemExit(1)
tracker.finish({"loss": config["rate"]})
"""


@pytest.fixture(scope="module")
def stochastic_scenario(tmp_path_factory, request):
    tmp_path = tmp_path_factory.mktemp("stochastic-retry")
    base_url, db_path, server, thread, store = _start_server(tmp_path)
    request.addfinalizer(lambda: _stop_server(server, thread, store))
    scenario = _scenario(
        tmp_path,
        request,
        base_url,
        STOCHASTIC_CONFIG,
        STOCHASTIC_TRIAL,
        n_trials=2,
        grid=None,
    )
    scenario.db_path = db_path
    return scenario


class TestStochasticFailUnchanged:
    def test_fail_refills_fresh_without_lineage_or_ledger(self, stochastic_scenario):
        s = stochastic_scenario
        assert s.results == [
            PipelineResult.RETRY_SUBMITTED,
            PipelineResult.SWEEP_COMPLETE,
        ]
        assert [spec.n_trials for spec in s.submitted_specs] == [1]
        assert s.ledger == {}

        trials = s.study.trials
        failed = [t for t in trials if t.state == TrialState.FAIL]
        assert len(failed) == 1
        assert all(
            t.user_attrs.get("retry_of") is None
            and t.user_attrs.get("retry_root") is None
            and t.user_attrs.get("retry_index") is None
            for t in trials
        )
        assert sum(1 for t in trials if t.params == failed[0].params) == 1
        assert sum(1 for t in trials if t.state == TrialState.COMPLETE) == 2

    def test_server_shows_single_failure(self, stochastic_scenario):
        s = stochastic_scenario
        states = dict(
            _sql(s.db_path, "SELECT state, COUNT(*) FROM trials GROUP BY state")
        )
        assert states == {"completed": 2, "failed": 1}
        failures = _sql(
            s.db_path,
            "SELECT COUNT(*) FROM executions WHERE outcome = 'failure'",
        )
        assert failures[0][0] == 1
