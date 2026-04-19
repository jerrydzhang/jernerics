from __future__ import annotations

from unittest.mock import patch

from jernerics.dag import DAG, task
from jernerics.dag.dag import _flatten_dict, _log_results_as_metrics


class TestFlattenDict:
    def test_flat_dict(self):
        result = _flatten_dict({"a": 1, "b": "hello"})
        assert result == {"a": "1", "b": "hello"}

    def test_nested_dict(self):
        result = _flatten_dict({"model": {"layers": 3, "hidden": 64}})
        assert result == {"model.layers": "3", "model.hidden": "64"}

    def test_deeply_nested(self):
        result = _flatten_dict({"a": {"b": {"c": 1}}})
        assert result == {"a.b.c": "1"}

    def test_empty_dict(self):
        result = _flatten_dict({})
        assert result == {}

    def test_mixed_types(self):
        result = _flatten_dict({"lr": 0.001, "name": "test", "flag": True})
        assert result == {"lr": "0.001", "name": "test", "flag": "True"}


class TestLogResultsAsMetrics:
    @patch("mlflow.log_metric")
    def test_dict_results(self, mock_log_metric):
        results = {"train": {"loss": 0.5, "acc": 0.9}, "eval": {"f1": 0.8}}
        _log_results_as_metrics(results)

        mock_log_metric.assert_any_call("train.loss", 0.5)
        mock_log_metric.assert_any_call("train.acc", 0.9)
        mock_log_metric.assert_any_call("eval.f1", 0.8)
        assert mock_log_metric.call_count == 3

    @patch("mlflow.log_metric")
    def test_scalar_results(self, mock_log_metric):
        results = {"score": 42.0}
        _log_results_as_metrics(results)

        mock_log_metric.assert_called_once_with("score", 42.0)

    @patch("mlflow.log_metric")
    def test_exception_results_skipped(self, mock_log_metric):
        results = {"good": 1.0, "bad": ValueError("oops")}
        _log_results_as_metrics(results)

        mock_log_metric.assert_called_once_with("good", 1.0)

    @patch("mlflow.log_metric")
    def test_non_numeric_dict_values_skipped(self, mock_log_metric):
        results = {"train": {"loss": 0.5, "name": "hello"}}
        _log_results_as_metrics(results)

        mock_log_metric.assert_called_once_with("train.loss", 0.5)

    @patch("mlflow.log_metric")
    def test_empty_results(self, mock_log_metric):
        _log_results_as_metrics({})
        mock_log_metric.assert_not_called()


class TestDAGRunMlflow:
    def test_no_mlflow_without_project_name(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        dag_file.write_text("pass")

        @task
        def my_task(config):
            return 1

        dag = DAG(dag_file)
        dag.add_task(my_task)

        with (
            patch("mlflow.set_experiment") as mock_set_exp,
            patch("mlflow.start_run") as mock_start_run,
        ):
            results = dag.run({})
            assert results["my_task"] == 1
            mock_set_exp.assert_not_called()
            mock_start_run.assert_not_called()

    def test_mlflow_with_project_name(self, tmp_path):
        mlflow_tracking_uri = f"file://{tmp_path / 'mlruns'}"

        dag_file = tmp_path / "dag.py"
        dag_file.write_text("pass")

        @task
        def my_task(config):
            return {"loss": 0.5, "acc": 0.9}

        dag = DAG(dag_file, project_name="test-project")
        dag.add_task(my_task)

        import mlflow

        mlflow.set_tracking_uri(mlflow_tracking_uri)

        results = dag.run({"lr": 0.001})

        assert results["my_task"]["loss"] == 0.5

        experiment = mlflow.get_experiment_by_name("test-project/dag")
        assert experiment is not None

        runs = mlflow.search_runs(experiment_ids=[experiment.experiment_id])
        assert len(runs) == 1
        assert runs.iloc[0]["params.lr"] == "0.001"
        assert runs.iloc[0]["metrics.my_task.loss"] == 0.5
        assert runs.iloc[0]["metrics.my_task.acc"] == 0.9

    def test_existing_dag_tests_still_pass(self, tmp_path):
        @task
        def a(config):
            return 1

        @task(depends_on=[a])
        def b(a, config):
            return a + 1

        dag = DAG()
        dag.add_task(a)
        dag.add_task(b)

        results = dag.run({})
        assert results["a"] == 1
        assert results["b"] == 2
