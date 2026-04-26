from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from jernerics.dag.task import Task, task


class TestTaskDecorator:
    def test_task_decorator_returns_task(self):
        @task
        def my_func(config):
            return 42

        assert isinstance(my_func, Task)
        assert my_func.name == "my_func"

    def test_task_decorator_with_dependencies(self):
        @task
        def dep_a(config):
            return "a"

        @task
        def dep_b(config):
            return "b"

        @task(depends_on=[dep_a, dep_b])
        def my_func(dep_a, dep_b, config):
            return f"{dep_a}-{dep_b}"

        assert isinstance(my_func, Task)
        assert len(my_func.depends_on) == 2
        assert my_func.depends_on[0].name == "dep_a"
        assert my_func.depends_on[1].name == "dep_b"

    def test_task_is_callable(self):
        @task
        def my_func(config):
            return 42

        result = my_func(config={})
        assert result == 42

    def test_task_preserves_docstring(self):
        @task
        def my_func(config):
            """This is a docstring."""
            return 42

        assert my_func.__doc__ == "This is a docstring."

    def test_task_can_be_called_with_args(self):
        @task
        def add(a, b, config):
            return a + b

        result = add(1, 2, config={})
        assert result == 3

    def test_task_can_be_called_with_kwargs(self):
        @task
        def greet(name, config):
            return f"Hello, {name}"

        result = greet(name="World", config={})
        assert result == "Hello, World"

    def test_task_empty_dependencies_by_default(self):
        @task
        def standalone(config):
            return True

        assert standalone.depends_on == []

    def test_task_dependencies_are_copied(self):
        @task
        def dep(config):
            return 1

        deps = [dep]

        @task(depends_on=deps)
        def my_func(dep, config):
            return dep

        deps.clear()
        assert len(my_func.depends_on) == 1

    @given(st.text(min_size=1, max_size=50))
    def test_task_name_preserved(self, name):
        def custom_func(config):
            return name

        custom_func.__name__ = name
        t = task(custom_func)

        assert t.name == name

    @given(st.integers(), st.integers())
    def test_task_with_various_inputs(self, a, b):
        @task
        def compute(a, b, config):
            return a + b

        result = compute(a, b, config={})
        assert result == a + b


class TestTaskDataclass:
    def test_task_is_dataclass(self):
        @task
        def my_func(config):
            return 1

        assert hasattr(my_func, "name")
        assert hasattr(my_func, "func")
        assert hasattr(my_func, "depends_on")

    def test_task_equality_by_identity(self):
        @task
        def func_a(config):
            return 1

        @task
        def func_b(config):
            return 1

        assert func_a is not func_b
