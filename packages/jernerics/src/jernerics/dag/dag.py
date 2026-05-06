import runpy
import types
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any, Self

from jernerics.tracking import Tracker

from .executor import Runner, TaskResult, ThreadPoolRunner, execute_dag
from .task import Task


class DAG:
    def __init__(
        self,
        dag_file: str | Path | None = None,
        project_name: str | None = None,
    ):
        self.tasks: dict[str, Task] = {}
        self.dag_file: Path | None = (
            Path(dag_file) if dag_file and str(dag_file).strip() else None
        )
        self.project_name = project_name
        self._discovered = False
        self._token = None

    def __enter__(self) -> Self:
        from .task import _active_dag

        self._token = _active_dag.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        from .task import _active_dag

        if self._token is not None:
            _active_dag.reset(self._token)
            self._token = None

    def add_task(self, task: Task) -> None:
        if task.name in self.tasks:
            raise ValueError(f"Task '{task.name}' is already registered")
        self.tasks[task.name] = task

    def _discover_tasks(self) -> None:
        if self._discovered:
            return
        self._discovered = True

        if not self.dag_file or not self.dag_file.exists():
            return

        try:
            module_ns = runpy.run_path(str(self.dag_file))
        except (SyntaxError, ImportError, PermissionError) as e:
            raise RuntimeError(f"Failed to load DAG file '{self.dag_file}': {e}") from e

        for obj in module_ns.values():
            if isinstance(obj, Task) and obj.name not in self.tasks:
                self.add_task(obj)

        for obj in module_ns.values():
            if isinstance(obj, DAG) and obj is not self:
                for task in obj.tasks.values():
                    if task.name not in self.tasks:
                        self.add_task(task)

    def _ensure_discovered(self) -> None:
        self._discover_tasks()

    def _build_graph(self) -> dict[str, set[str]]:
        self._ensure_discovered()
        graph: dict[str, set[str]] = {}

        for task_name, task in self.tasks.items():
            dep_names: set[str] = set()
            for dep in task.depends_on:
                if dep.name not in self.tasks:
                    raise ValueError(
                        f"Task '{task_name}' depends on unregistered task '{dep.name}'"
                    )
                dep_names.add(dep.name)
            graph[task_name] = dep_names

        return graph

    def validate(self) -> None:
        graph = self._build_graph()
        sorter = TopologicalSorter(graph)
        sorter.prepare()

    def run(
        self,
        config: dict[str, Any],
        tracker: Tracker | None = None,
        runner: Runner | None = None,
    ) -> dict[str, TaskResult]:
        self._ensure_discovered()

        return execute_dag(
            self.tasks,
            config,
            runner=runner or ThreadPoolRunner(max_workers=None),
            tracker=tracker,
        )
