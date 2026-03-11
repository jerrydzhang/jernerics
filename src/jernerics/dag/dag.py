from __future__ import annotations

import runpy
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

from .executor import execute_dag
from .state import RunState
from .task import Task


class DAG:
    def __init__(self, dag_file: str | Path | None = None):
        self.tasks: dict[str, Task] = {}
        self.dag_file: Path | None = Path(dag_file) if dag_file else None
        self.state_dir: Path | None = None
        self._discovered = False

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

        module_ns = runpy.run_path(str(self.dag_file))

        for name, obj in module_ns.items():
            if isinstance(obj, Task):
                self.add_task(obj)

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
        config_index: int = 0,
        state_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        self.validate()

        if state_dir is not None:
            self.state_dir = Path(state_dir)
        elif self.dag_file:
            self.state_dir = self.dag_file.parent / ".jernerics"

        state: RunState | None = None
        if self.state_dir:
            dag_file_name = str(self.dag_file) if self.dag_file else "inline"
            state = RunState.create(
                dag_file=dag_file_name,
                config_index=config_index,
                state_dir=self.state_dir,
            )
            for task_name in self.tasks:
                state.init_task(task_name)

        return execute_dag(self.tasks, config, state=state)

    def resume(
        self,
        config: dict[str, Any],
        config_index: int = 0,
        run_id: str | None = None,
        state_dir: Path | str | None = None,
    ) -> dict[str, Any]:
        self.validate()

        if state_dir is not None:
            self.state_dir = Path(state_dir)
        elif self.dag_file:
            self.state_dir = self.dag_file.parent / ".jernerics"
        else:
            raise ValueError("No state directory available for resume")

        if not self.state_dir or not self.state_dir.exists():
            raise ValueError(f"No state directory found at {self.state_dir}")

        if run_id is not None:
            state_file = self.state_dir / "runs" / f"{run_id}_{config_index}.json"
            if not state_file.exists():
                raise ValueError(f"Run {run_id} not found at {state_file}")
            state = RunState.from_json(state_file)
        else:
            state = RunState.get_latest_run(self.state_dir, config_index)
            if state is None:
                raise ValueError(f"No previous runs found in {self.state_dir}")

        from .state import TaskStatus

        for task_name, task_state in state.tasks.items():
            if task_state.status == TaskStatus.RUNNING:
                state.update_task(task_name, TaskStatus.PENDING)

        return execute_dag(self.tasks, config, state=state)
