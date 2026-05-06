import sqlite3

import optuna
import pytest
from jernerics.backend.local_backend import LocalBackend
from jernerics.backend.models import SweepSubmission
from jernerics.tracking.batch_sync import replay_tracking, sync_artifacts
from optuna.storages.journal import JournalFileBackend, JournalStorage

STUDY_NAME = "test-sweep"


def _write_dag(path, body, tracker_param=""):
    """Write a minimal DAG file with a single train task.

    tracker_param: set to ", tracker" to inject the tracker into the task.
    """
    path.write_text(
        "from jernerics.dag import task\n\n"
        "@task\n"
        f"def train(config{tracker_param}):\n"
        f"    {body}\n"
    )


def _write_config(path, base=None, n_trials=2, objective_expr=None):
    lines = [f"base = {base or {'lr': 0.01}}"]
    lines.append(f"n_trials = {n_trials}")
    if objective_expr:
        lines.append(f"def objective(results):\n    return {objective_expr}")
    lines.append("backend_overrides = {}")
    path.write_text("\n".join(lines) + "\n")


def _make_spec(tmp_path, dag_file, config_file, n_trials=2):
    journal_dir = tmp_path / "optuna"
    journal_dir.mkdir(exist_ok=True)
    return SweepSubmission(
        dag_path=dag_file,
        config_path=config_file,
        study_name=STUDY_NAME,
        storage_url=str(journal_dir / f"{STUDY_NAME}.journal"),
        n_trials=n_trials,
        tracking_dir=tmp_path / "tracking" / STUDY_NAME,
    )


class TestBasicSweep:
    def test_optuna_study_has_correct_trials_and_objectives(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        config_file = tmp_path / "config.py"

        _write_dag(dag_file, 'return {"loss": config["lr"] * 2}')
        _write_config(config_file, objective_expr='results["train"]["loss"]')

        spec = _make_spec(tmp_path, dag_file, config_file, n_trials=2)
        backend = LocalBackend()
        result = backend.submit_sweep(spec)

        assert len(result.submissions) == 1
        assert result.submissions[0].n_trials == 2

        storage = JournalStorage(JournalFileBackend(spec.storage_url))
        study = optuna.load_study(study_name=STUDY_NAME, storage=storage)
        assert len(study.trials) == 2
        assert all(t.state == optuna.trial.TrialState.COMPLETE for t in study.trials)
        assert all(abs(t.value - 0.02) < 1e-6 for t in study.trials)


class TestArtifactRoundTrip:
    def test_artifacts_uploaded_to_s3_with_correct_keys(self, tmp_path, monkeypatch):
        import boto3
        from moto import mock_aws

        monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

        artifact_dir = tmp_path / "artifact_data"
        artifact_dir.mkdir()

        dag_file = tmp_path / "dag.py"
        config_file = tmp_path / "config.py"

        _write_dag(
            dag_file,
            "import os\n"
            '    path = os.path.join(config["artifact_dir"], "model.txt")\n'
            '    with open(path, "w") as f:\n'
            '        f.write("model data")\n'
            "    tracker.log_artifact('model', path)\n"
            '    return {"loss": config["lr"] * 2}',
            tracker_param=", tracker",
        )
        _write_config(
            config_file,
            base={"lr": 0.01, "artifact_dir": str(artifact_dir)},
            n_trials=2,
            objective_expr='results["train"]["loss"]',
        )

        spec = _make_spec(tmp_path, dag_file, config_file, n_trials=2)
        backend = LocalBackend()
        backend.submit_sweep(spec)

        with mock_aws():
            s3 = boto3.client("s3", region_name="us-east-1")
            bucket = "test-artifacts"
            s3.create_bucket(Bucket=bucket)

            def upload_file(s3_key, local_path):
                s3.upload_file(local_path, bucket, s3_key)

            sync_artifacts(
                tracking_dir=tmp_path / "tracking",
                upload_fn=upload_file,
                project="test-project",
                study=STUDY_NAME,
            )

            objects = s3.list_objects_v2(Bucket=bucket)
            keys = sorted(obj["Key"] for obj in objects["Contents"])
            assert keys == [
                "test-project/test-sweep/0/model",
                "test-project/test-sweep/1/model",
            ]

            body = (
                s3.get_object(Bucket=bucket, Key="test-project/test-sweep/0/model")[
                    "Body"
                ]
                .read()
                .decode()
            )
            assert body == "model data"


class TestTrackingReplayRoundTrip:
    def test_replay_sends_all_event_types_to_sqlite(self, tmp_path, grpc_server):
        stub, db_path, _ = grpc_server

        dag_file = tmp_path / "dag.py"
        config_file = tmp_path / "config.py"

        _write_dag(
            dag_file,
            'lr = config["lr"]\n'
            "    tracker.log_param('lr', lr)\n"
            "    tracker.log_metric('loss', lr * 2, step=1)\n"
            "    tracker.log_result('summary', {'accuracy': 0.95})\n"
            '    return {"loss": lr * 2}',
            tracker_param=", tracker",
        )
        _write_config(config_file, objective_expr='results["train"]["loss"]')

        spec = _make_spec(tmp_path, dag_file, config_file, n_trials=2)
        backend = LocalBackend()
        backend.submit_sweep(spec)

        # Replay .pb files to gRPC server
        tracking_parent = (tmp_path / "tracking" / STUDY_NAME).parent
        replay_tracking(tracking_dir=tracking_parent, stub=stub, study=STUDY_NAME)

        # Verify all event types in SQLite
        con = sqlite3.connect(str(db_path))
        assert con.execute("SELECT COUNT(*) FROM params").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM trial_end").fetchone()[0] == 2
        con.close()


class TestSweepFailure:
    def test_failed_trial_raises_runtime_error(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        config_file = tmp_path / "config.py"

        _write_dag(dag_file, 'raise ValueError("boom")')
        _write_config(config_file, n_trials=1)

        spec = _make_spec(tmp_path, dag_file, config_file, n_trials=1)
        backend = LocalBackend()

        with pytest.raises(RuntimeError, match="One or more trials failed"):
            backend.submit_sweep(spec)
