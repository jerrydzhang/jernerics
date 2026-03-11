from __future__ import annotations

import concurrent.futures
from concurrent.futures import ALL_COMPLETED
from datetime import datetime, timezone
from graphlib import TopologicalSorter
from typing import TYPE_CHECKING, Any

from .task import Task

if TYPE_CHECKING:
    from .state import RunState, TaskStatus


def _get_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execute_dag(
    tasks: dict[str, Task],
    config: dict[str, Any],
    state: RunState | None = None,
) -> dict[str, Any]:
    graph: dict[str, set[str]] = {}
    for task_name, task in tasks.items():
        dep_names = {dep.name for dep in task.depends_on if dep.name in tasks}
        graph[task_name] = dep_names

    results: dict[str, Any] = {}
    failed_tasks: set[str] = set()

    if state:
        for task_name, task_state in state.tasks.items():
            from .state import TaskStatus

            if task_state.status == TaskStatus.FAILED:
                state.update_task(task_name, TaskStatus.PENDING)

    sorter = TopologicalSorter(graph)
    sorter.prepare()

    with concurrent.futures.ThreadPoolExecutor() as executor:
        while sorter.is_active():
            ready_tasks = sorter.get_ready()

            if not ready_tasks:
                break

            futures: dict[concurrent.futures.Future[Any], str] = {}
            for task_name in ready_tasks:
                task = tasks[task_name]

                if state and task_name in state.tasks:
                    from .state import TaskStatus

                    task_state = state.tasks[task_name]
                    if task_state.status == TaskStatus.COMPLETED:
                        results[task_name] = task_state.output
                        sorter.done(task_name)
                        continue

                upstream_failed = any(
                    dep.name in failed_tasks for dep in task.depends_on
                )

                if upstream_failed:
                    results[task_name] = Exception(
                        f"Upstream task(s) failed for '{task_name}'"
                    )
                    failed_tasks.add(task_name)
                    sorter.done(task_name)

                    if state:
                        from .state import TaskStatus

                        state.update_task(
                            task_name,
                            TaskStatus.FAILED,
                            error="Upstream task(s) failed",
                        )
                        state.to_json()
                else:
                    inputs: dict[str, Any] = {}
                    for dep in task.depends_on:
                        if dep.name in results:
                            dep_result = results[dep.name]
                            if not isinstance(dep_result, Exception):
                                inputs[dep.name] = dep_result

                    if state:
                        from .state import TaskStatus

                        state.update_task(
                            task_name, TaskStatus.RUNNING, started_at=_get_timestamp()
                        )
                        state.to_json()

                    future = executor.submit(_run_task, task, inputs, config)
                    futures[future] = task_name

            if futures:
                completed, _ = concurrent.futures.wait(
                    futures, return_when=ALL_COMPLETED
                )

                for future in completed:
                    task_name = futures[future]
                    try:
                        result = future.result()
                        results[task_name] = result

                        if state:
                            from .state import TaskStatus

                            state.update_task(
                                task_name,
                                TaskStatus.COMPLETED,
                                completed_at=_get_timestamp(),
                                output=result,
                            )
                            state.to_json()
                    except Exception as e:
                        results[task_name] = e
                        failed_tasks.add(task_name)

                        if state:
                            from .state import TaskStatus

                            state.update_task(
                                task_name,
                                TaskStatus.FAILED,
                                completed_at=_get_timestamp(),
                                error=str(e),
                            )
                            state.to_json()

                    sorter.done(task_name)

    return results


def _run_task(task: Task, inputs: dict[str, Any], config: dict[str, Any]) -> Any:
    kwargs = {**inputs, "config": config}
    return task.func(**kwargs)
