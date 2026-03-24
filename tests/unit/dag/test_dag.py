from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from jernerics.dag import DAG, task


class TestDAGCreation:
    def test_dag_empty_creation(self):
        dag = DAG()
        assert dag.tasks == {}
        assert dag.dag_file is None

    def test_dag_with_file(self, tmp_path):
        dag_file = tmp_path / "dag.py"
        dag_file.write_text("pass")

        dag = DAG(dag_file)
        assert dag.dag_file == dag_file

    def test_dag_add_task(self):
        @task
        def my_task(config):
            return 1

        dag = DAG()
        dag.add_task(my_task)

        assert "my_task" in dag.tasks
        assert dag.tasks["my_task"] is my_task

    def test_dag_add_duplicate_task_raises(self):
        @task
        def my_task(config):
            return 1

        dag = DAG()
        dag.add_task(my_task)

        try:
            dag.add_task(my_task)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "already registered" in str(e)

    def test_dag_context_manager_auto_registers_tasks(self):
        with DAG() as dag:

            @task
            def task_a(config):
                return 1

            @task
            def task_b(config):
                return 2

        assert "task_a" in dag.tasks
        assert "task_b" in dag.tasks

    def test_dag_context_manager_nested(self):
        with DAG() as dag1:

            @task
            def task_a(config):
                return 1

            with DAG() as dag2:

                @task
                def task_b(config):
                    return 2

            @task
            def task_c(config):
                return 3

        assert "task_a" in dag1.tasks
        assert "task_c" in dag1.tasks
        assert "task_b" not in dag1.tasks
        assert "task_b" in dag2.tasks

    def test_dag_no_auto_register_outside_context(self):
        @task
        def standalone_task(config):
            return 1

        dag = DAG()
        assert "standalone_task" not in dag.tasks


class TestDAGDiscovery:
    def test_discover_tasks_from_file(self, tmp_path):
        dag_content = """
from jernerics.dag import task

@task
def task_a(config):
    return "a"

@task
def task_b(config):
    return "b"
"""
        dag_file = tmp_path / "dag.py"
        dag_file.write_text(dag_content)

        dag = DAG(dag_file)
        dag.validate()

        assert "task_a" in dag.tasks
        assert "task_b" in dag.tasks

    def test_discover_tasks_with_dependencies(self, tmp_path):
        dag_content = """
from jernerics.dag import task

@task
def task_a(config):
    return "a"

@task(depends_on=[task_a])
def task_b(task_a, config):
    return f"b from {task_a}"
"""
        dag_file = tmp_path / "dag.py"
        dag_file.write_text(dag_content)

        dag = DAG(dag_file)
        dag.validate()

        assert "task_a" in dag.tasks
        assert "task_b" in dag.tasks
        assert dag.tasks["task_b"].depends_on[0].name == "task_a"

    def test_discover_only_runs_once(self, tmp_path):
        dag_content = """
from jernerics.dag import task

@task
def my_task(config):
    return 1
"""
        dag_file = tmp_path / "dag.py"
        dag_file.write_text(dag_content)

        dag = DAG(dag_file)
        dag.validate()
        dag.validate()
        dag.validate()

        assert len(dag.tasks) == 1

    def test_discover_nonexistent_file(self):
        dag = DAG("/nonexistent/path/dag.py")
        dag.validate()

        assert dag.tasks == {}

    def test_no_infinite_recursion(self, tmp_path):
        dag_content = """
from jernerics.dag import task, DAG

@task
def my_task(config):
    return 1

dag = DAG(__file__)
"""
        dag_file = tmp_path / "dag.py"
        dag_file.write_text(dag_content)

        dag = DAG(dag_file)
        dag.validate()

        assert "my_task" in dag.tasks


class TestDAGValidation:
    def test_validate_empty_dag(self):
        dag = DAG()
        dag.validate()

    def test_validate_single_task(self):
        @task
        def single(config):
            return 1

        dag = DAG()
        dag.add_task(single)
        dag.validate()

    def test_validate_linear_chain(self):
        @task
        def a(config):
            return 1

        @task(depends_on=[a])
        def b(a, config):
            return a + 1

        @task(depends_on=[b])
        def c(b, config):
            return b + 1

        dag = DAG()
        dag.add_task(a)
        dag.add_task(b)
        dag.add_task(c)
        dag.validate()

    def test_validate_diamond_dependency(self):
        @task
        def a(config):
            return 1

        @task(depends_on=[a])
        def b(a, config):
            return a + 1

        @task(depends_on=[a])
        def c(a, config):
            return a + 2

        @task(depends_on=[b, c])
        def d(b, c, config):
            return b + c

        dag = DAG()
        dag.add_task(a)
        dag.add_task(b)
        dag.add_task(c)
        dag.add_task(d)
        dag.validate()

    def test_validate_missing_dependency_raises(self):
        @task
        def a(config):
            return 1

        @task
        def b(config):
            return 2

        @task(depends_on=[b])
        def c(b, config):
            return b + 1

        dag = DAG()
        dag.add_task(a)
        dag.add_task(c)

        try:
            dag.validate()
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "unregistered task" in str(e)


class TestDAGExecution:
    def test_run_single_task(self):
        @task
        def my_task(config):
            return config["value"] * 2

        dag = DAG()
        dag.add_task(my_task)

        results = dag.run({"value": 21})

        assert results["my_task"] == 42

    def test_run_linear_chain(self):
        @task
        def a(config):
            return 1

        @task(depends_on=[a])
        def b(a, config):
            return a + 1

        @task(depends_on=[b])
        def c(b, config):
            return b + 1

        dag = DAG()
        dag.add_task(a)
        dag.add_task(b)
        dag.add_task(c)

        results = dag.run({})

        assert results["a"] == 1
        assert results["b"] == 2
        assert results["c"] == 3

    def test_run_diamond_dependency(self):
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

        dag = DAG()
        dag.add_task(a)
        dag.add_task(b)
        dag.add_task(c)
        dag.add_task(d)

        results = dag.run({})

        assert results["a"] == 1
        assert results["b"] == 11
        assert results["c"] == 101
        assert results["d"] == 112

    def test_run_injects_config(self):
        @task
        def my_task(config):
            return config["key"]

        dag = DAG()
        dag.add_task(my_task)

        results = dag.run({"key": "value"})

        assert results["my_task"] == "value"

    def test_run_creates_state_directory(self, tmp_path):
        @task
        def my_task(config):
            return 1

        dag_file = tmp_path / "dag.py"
        dag_file.write_text("pass")

        dag = DAG(dag_file)
        dag.add_task(my_task)
        dag.run({})

        state_dir = tmp_path / ".jernerics"
        assert state_dir.exists()

    def test_run_state_persistence(self, tmp_path):
        @task
        def my_task(config):
            return 42

        dag_file = tmp_path / "dag.py"
        dag_file.write_text("pass")

        dag = DAG(dag_file)
        dag.add_task(my_task)
        dag.run({}, config_index=5)

        state_file = tmp_path / ".jernerics" / "runs" / "latest_5.json"
        assert state_file.exists()

    @given(st.integers(), st.integers())
    def test_run_with_config_values(self, x, y):
        @task
        def add(config):
            return config["x"] + config["y"]

        dag = DAG()
        dag.add_task(add)

        results = dag.run({"x": x, "y": y})

        assert results["add"] == x + y


class TestDAGErrorHandling:
    def test_task_exception_recorded(self):
        @task
        def failing_task(config):
            raise ValueError("intentional error")

        dag = DAG()
        dag.add_task(failing_task)

        results = dag.run({})

        assert isinstance(results["failing_task"], ValueError)

    def test_downstream_task_skipped_on_failure(self):
        @task
        def failing(config):
            raise ValueError("intentional error")

        @task(depends_on=[failing])
        def downstream(failing, config):
            return "should not run"

        dag = DAG()
        dag.add_task(failing)
        dag.add_task(downstream)

        results = dag.run({})

        assert isinstance(results["failing"], ValueError)
        assert isinstance(results["downstream"], Exception)
        assert "Upstream" in str(results["downstream"])

    def test_independent_tasks_run_on_partial_failure(self):
        @task
        def failing(config):
            raise ValueError("intentional error")

        @task
        def independent(config):
            return "success"

        dag = DAG()
        dag.add_task(failing)
        dag.add_task(independent)

        results = dag.run({})

        assert isinstance(results["failing"], ValueError)
        assert results["independent"] == "success"


class TestDAGResume:
    def test_resume_from_state(self, tmp_path):
        @task
        def slow_task(config):
            return 42

        dag_file = tmp_path / "dag.py"
        dag_file.write_text("pass")

        dag = DAG(dag_file)
        dag.add_task(slow_task)
        dag.run({}, config_index=0)

        dag2 = DAG(dag_file)
        dag2.add_task(slow_task)
        results = dag2.resume({}, config_index=0)

        assert results["slow_task"] == 42
