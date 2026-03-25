from __future__ import annotations

import contextlib
import os
import time
import warnings

from hypothesis import given
from hypothesis import strategies as st

from jernerics.dag.executor import _get_default_max_workers, _run_task, execute_dag
from jernerics.dag.state import RunState, TaskStatus
from jernerics.dag.task import task


class TestDefaultMaxWorkers:
    def test_default_max_workers_reasonable(self):
        workers = _get_default_max_workers()
        assert workers >= 4
        assert workers == (os.cpu_count() or 4)

    def test_default_max_workers_used_when_none(self):
        call_count = 0
        original_submit = None

        @task
        def simple_task(config):
            return 1

        import concurrent.futures
        from unittest.mock import patch

        with patch.object(
            concurrent.futures.ThreadPoolExecutor, "__init__", autospec=True
        ) as mock_init:
            mock_init.return_value = None

            with contextlib.suppress(Exception):
                execute_dag({"simple": simple_task}, {})

            call_args = mock_init.call_args
            assert call_args is not None
            assert "max_workers" in call_args.kwargs
            assert call_args.kwargs["max_workers"] == _get_default_max_workers()


class TestRunTask:
    def test_run_task_basic(self):
        @task
        def my_task(config):
            return config["value"]

        result = _run_task(my_task, {}, {"value": 42})
        assert result == 42

    def test_run_task_with_inputs(self):
        @task
        def my_task(upstream, config):
            return upstream * 2

        result = _run_task(my_task, {"upstream": 10}, {})
        assert result == 20

    def test_run_task_merges_inputs_and_config(self):
        @task
        def my_task(data, config):
            return data + config["add"]

        result = _run_task(my_task, {"data": 5}, {"add": 7})
        assert result == 12

    @given(st.integers(), st.integers())
    def test_run_task_various_inputs(self, a, b):
        @task
        def compute(x, y, config):
            return x + y

        result = _run_task(compute, {"x": a, "y": b}, {})
        assert result == a + b


class TestExecuteDAG:
    def test_execute_single_task(self):
        @task
        def single(config):
            return 1

        tasks = {"single": single}
        results = execute_dag(tasks, {})

        assert results["single"] == 1

    def test_execute_parallel_tasks(self):
        @task
        def task_a(config):
            time.sleep(0.01)
            return "a"

        @task
        def task_b(config):
            time.sleep(0.01)
            return "b"

        tasks = {"task_a": task_a, "task_b": task_b}

        start = time.time()
        results = execute_dag(tasks, {})
        elapsed = time.time() - start

        assert results["task_a"] == "a"
        assert results["task_b"] == "b"
        assert elapsed < 0.05

    def test_execute_respects_dependencies(self):
        execution_order = []

        @task
        def first(config):
            execution_order.append("first")
            return 1

        @task(depends_on=[first])
        def second(first, config):
            execution_order.append("second")
            return first + 1

        tasks = {"first": first, "second": second}
        results = execute_dag(tasks, {})

        assert execution_order == ["first", "second"]
        assert results["first"] == 1
        assert results["second"] == 2

    def test_execute_with_state(self, tmp_path):
        @task
        def my_task(config):
            return 42

        tasks = {"my_task": my_task}
        state = RunState.create("test_dag.py", 0, tmp_path)
        state.init_task("my_task")

        results = execute_dag(tasks, {}, state=state)

        assert results["my_task"] == 42
        assert state.tasks["my_task"].status == TaskStatus.COMPLETED
        assert state.tasks["my_task"].output == 42

    def test_execute_skips_completed_tasks(self):
        call_count = 0

        @task
        def expensive(config):
            nonlocal call_count
            call_count += 1
            return 999

        tasks = {"expensive": expensive}
        state = RunState.create("test_dag.py", 0, ".jernerics")
        state.init_task("expensive")
        state.update_task("expensive", TaskStatus.COMPLETED, output=42)

        results = execute_dag(tasks, {}, state=state)

        assert call_count == 0
        assert results["expensive"] == 42

    def test_execute_reruns_non_persisted_tasks(self, tmp_path):
        import socket

        call_count = 0

        @task
        def non_serializable(config):
            nonlocal call_count
            call_count += 1
            sock = socket.socket()
            return sock

        tasks = {"non_serializable": non_serializable}
        state = RunState.create("test_dag.py", 0, tmp_path)
        state.init_task("non_serializable")

        sock = socket.socket()
        try:
            state.update_task("non_serializable", TaskStatus.COMPLETED, output=sock)
            state.tasks["non_serializable"].persisted = False

            results = execute_dag(tasks, {}, state=state)

            assert call_count == 1
        finally:
            sock.close()

    def test_execute_records_failures(self, tmp_path):
        @task
        def failing(config):
            raise ValueError("boom")

        tasks = {"failing": failing}
        state = RunState.create("test_dag.py", 0, tmp_path)
        state.init_task("failing")

        results = execute_dag(tasks, {}, state=state)

        assert isinstance(results["failing"], ValueError)
        assert state.tasks["failing"].status == TaskStatus.FAILED
        assert "boom" in state.tasks["failing"].error

    def test_execute_complex_dag(self):
        @task
        def a(config):
            return 1

        @task(depends_on=[a])
        def b(a, config):
            return a + 10

        @task(depends_on=[a])
        def c(a, config):
            return a + 100

        @task(depends_on=[b, c])
        def d(b, c, config):
            return b + c

        tasks = {"a": a, "b": b, "c": c, "d": d}
        results = execute_dag(tasks, {})

        assert results["a"] == 1
        assert results["b"] == 11
        assert results["c"] == 101
        assert results["d"] == 112

    def test_execute_failure_propagates(self):
        @task
        def failing(config):
            raise RuntimeError("error")

        @task(depends_on=[failing])
        def dependent(failing, config):
            return "should not run"

        tasks = {"failing": failing, "dependent": dependent}
        results = execute_dag(tasks, {})

        assert isinstance(results["failing"], RuntimeError)
        assert isinstance(results["dependent"], Exception)
        assert "Upstream" in str(results["dependent"])


class TestExecutorEdgeCases:
    def test_empty_dag(self):
        results = execute_dag({}, {})
        assert results == {}

    def test_many_independent_tasks(self):
        num_tasks = 20

        tasks = {}
        for i in range(num_tasks):

            @task
            def make_task(idx=i):
                def impl(config):
                    return idx

                impl.__name__ = f"task_{idx}"
                return task(impl)

            tasks[f"task_{i}"] = make_task()

        results = execute_dag(tasks, {})

        for i in range(num_tasks):
            assert results[f"task_{i}"] == i

    def test_long_chain(self):
        @task
        def t0(config):
            return 0

        @task(depends_on=[t0])
        def t1(t0, config):
            return t0 + 1

        @task(depends_on=[t1])
        def t2(t1, config):
            return t1 + 1

        @task(depends_on=[t2])
        def t3(t2, config):
            return t2 + 1

        @task(depends_on=[t3])
        def t4(t3, config):
            return t3 + 1

        tasks = {"t0": t0, "t1": t1, "t2": t2, "t3": t3, "t4": t4}
        results = execute_dag(tasks, {})

        assert results["t4"] == 4


class TestExecutorType:
    def test_invalid_executor_type_raises(self):
        @task
        def simple(config):
            return 1

        try:
            execute_dag({"simple": simple}, {}, executor_type="invalid")
            raise AssertionError("Should have raised ValueError")
        except ValueError as e:
            assert "executor_type must be 'thread' or 'serial'" in str(e)


class TestMaxWorkers:
    def test_invalid_max_workers_zero(self):
        @task
        def simple(config):
            return 1

        try:
            execute_dag({"simple": simple}, {}, max_workers=0)
            raise AssertionError("Should have raised ValueError")
        except ValueError as e:
            assert "max_workers must be a positive integer" in str(e)

    def test_invalid_max_workers_negative(self):
        @task
        def simple(config):
            return 1

        try:
            execute_dag({"simple": simple}, {}, max_workers=-1)
            raise AssertionError("Should have raised ValueError")
        except ValueError as e:
            assert "max_workers must be a positive integer" in str(e)

    def test_invalid_max_workers_string(self):
        @task
        def simple(config):
            return 1

        try:
            execute_dag({"simple": simple}, {}, max_workers="invalid")
            raise AssertionError("Should have raised ValueError")
        except ValueError as e:
            assert "max_workers must be a positive integer" in str(e)


class TestUnregisteredDependency:
    def test_unregistered_dependency_warning(self):
        @task
        def unregistered(config):
            return 1

        @task(depends_on=[unregistered])
        def main_task(unregistered, config):
            return unregistered + 1

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            results = execute_dag({"main_task": main_task}, {})

            assert len(w) == 1
            assert "unregistered task" in str(w[0].message)


class TestSerialExecutor:
    def test_serial_executor_runs_tasks(self):
        order = []

        @task
        def a(config):
            order.append("a")
            return 1

        @task
        def b(config):
            order.append("b")
            return 2

        tasks = {"a": a, "b": b}
        results = execute_dag(tasks, {}, executor_type="serial")

        assert results["a"] == 1
        assert results["b"] == 2

    def test_serial_executor_respects_dependencies(self):
        order = []

        @task
        def first(config):
            order.append("first")
            return 1

        @task(depends_on=[first])
        def second(first, config):
            order.append("second")
            return first + 1

        tasks = {"first": first, "second": second}
        results = execute_dag(tasks, {}, executor_type="serial")

        assert order == ["first", "second"]
        assert results["second"] == 2

    def test_serial_executor_with_state(self, tmp_path):
        @task
        def my_task(config):
            return 42

        tasks = {"my_task": my_task}
        state = RunState.create("test_dag.py", 0, tmp_path)
        state.init_task("my_task")

        results = execute_dag(tasks, {}, state=state, executor_type="serial")

        assert results["my_task"] == 42
        assert state.tasks["my_task"].status == TaskStatus.COMPLETED

    def test_serial_executor_skips_persisted(self):
        call_count = 0

        @task
        def cached(config):
            nonlocal call_count
            call_count += 1
            return 999

        tasks = {"cached": cached}
        state = RunState.create("test_dag.py", 0, ".jernerics")
        state.init_task("cached")
        state.update_task("cached", TaskStatus.COMPLETED, output=42)

        results = execute_dag(tasks, {}, state=state, executor_type="serial")

        assert call_count == 0
        assert results["cached"] == 42

    def test_serial_executor_handles_failure(self, tmp_path):
        @task
        def failing(config):
            raise ValueError("test error")

        tasks = {"failing": failing}
        state = RunState.create("test_dag.py", 0, tmp_path)
        state.init_task("failing")

        results = execute_dag(tasks, {}, state=state, executor_type="serial")

        assert isinstance(results["failing"], ValueError)
        assert state.tasks["failing"].status == TaskStatus.FAILED

    def test_serial_executor_upstream_failure(self, tmp_path):
        @task
        def failing(config):
            raise RuntimeError("boom")

        @task(depends_on=[failing])
        def dependent(failing, config):
            return "should not run"

        tasks = {"failing": failing, "dependent": dependent}
        state = RunState.create("test_dag.py", 0, tmp_path)
        state.init_task("failing")
        state.init_task("dependent")

        results = execute_dag(tasks, {}, state=state, executor_type="serial")

        assert isinstance(results["dependent"], Exception)
        assert state.tasks["dependent"].status == TaskStatus.FAILED


class TestStateReset:
    def test_failed_tasks_reset_on_rerun(self, tmp_path):
        call_count = 0

        @task
        def my_task(config):
            nonlocal call_count
            call_count += 1
            return 42

        tasks = {"my_task": my_task}
        state = RunState.create("test_dag.py", 0, tmp_path)
        state.init_task("my_task")
        state.update_task("my_task", TaskStatus.FAILED, error="previous error")

        results = execute_dag(tasks, {}, state=state)

        assert call_count == 1
        assert results["my_task"] == 42
        assert state.tasks["my_task"].status == TaskStatus.COMPLETED


class TestConfigParameterWarning:
    def test_config_parameter_override_warning(self):
        @task
        def my_task(upstream_config, config):
            return (config, upstream_config)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = _run_task(
                my_task,
                {"upstream_config": "upstream", "config": "will be overwritten"},
                {"key": "value"},
            )

            assert len(w) == 1
            assert "overwritten by the DAG config dict" in str(w[0].message)
            assert result == ({"key": "value"}, "upstream")
