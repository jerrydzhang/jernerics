import runpy
import types
import warnings
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any, Self

from jernerics.tracking import Tracker

from .executor import Runner, TaskResult, ThreadPoolRunner, execute_dag
from .provenance import Provenance
from .state import RunState, TaskStatus
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
        self.state_dir: Path | None = None
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
        config_index: int = 0,
        state_dir: Path | str | None = None,
        config_path: str | None = None,
        container_path: str | None = None,
        tracker: Tracker | None = None,
        runner: Runner | None = None,
    ) -> dict[str, TaskResult]:
        if state_dir is not None:
            self.state_dir = Path(state_dir)
        elif self.dag_file:
            self.state_dir = self.dag_file.parent / ".jernerics"

        state: RunState | None = None
        provenance: Provenance | None = None

        if self.state_dir:
            dag_file_name = str(self.dag_file) if self.dag_file else "inline"
            state = RunState.create(
                dag_file=dag_file_name,
                config_index=config_index,
                state_dir=self.state_dir,
            )
            for task_name in self.tasks:
                state.init_task(task_name)

            provenance = Provenance.create(
                run_id=state.run_id,
                config_path=config_path,
                container_path=container_path,
                repo_path=self.dag_file.parent if self.dag_file else None,
            )
            provenance.to_json(self.state_dir)

        exc_info: BaseException | None = None

        try:
            results = execute_dag(
                self.tasks,
                config,
                state=state,
                runner=runner or ThreadPoolRunner(max_workers=None),
                tracker=tracker,
            )
        except BaseException as e:
            exc_info = e
            raise
        finally:
            if provenance and self.state_dir:
                try:
                    provenance.finalize()
                    provenance.to_json(self.state_dir)
                except BaseException as e:
                    if exc_info is not None:
                        raise e from exc_info
                    raise

        return results

    def resume(
        self,
        config: dict[str, Any],
        config_index: int = 0,
        run_id: str | None = None,
        state_dir: Path | str | None = None,
        runner: Runner | None = None,
    ) -> dict[str, TaskResult]:
        self.validate()

        if state_dir is not None:
            self.state_dir = Path(state_dir)
        elif self.dag_file:
            self.state_dir = self.dag_file.parent / ".jernerics"
        else:
            raise ValueError("No state directory available for resume")

        if self.state_dir is None:
            raise ValueError(
                "No state directory configured. Specify state_dir or dag_file."
            )
        if not self.state_dir.exists():
            raise ValueError(f"State directory not found: {self.state_dir}")

        if run_id is not None:
            state_file = self.state_dir / "runs" / f"{run_id}_{config_index}.json"
            if not state_file.exists():
                raise ValueError(f"Run {run_id} not found at {state_file}")
            state = RunState.from_json(state_file)
        else:
            state = RunState.get_latest_run(self.state_dir, config_index)
            if state is None:
                raise ValueError(f"No previous runs found in {self.state_dir}")

        for task_name in state.tasks:
            if task_name not in self.tasks:
                warnings.warn(
                    f"Task '{task_name}' from saved state is not in the current DAG",
                    UserWarning,
                    stacklevel=2,
                )

        for task_name, task_state in state.tasks.items():
            if task_state.status == TaskStatus.RUNNING:
                state.update_task(task_name, TaskStatus.PENDING)

        return execute_dag(
            self.tasks,
            config,
            state=state,
            runner=runner or ThreadPoolRunner(max_workers=None),
        )
